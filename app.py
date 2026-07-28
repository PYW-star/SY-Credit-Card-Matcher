"""
Credit Card Claims Matcher
--------------------------
Flow: choose bank -> upload statement -> upload the receipts/invoices that
need to be matched against it (individual files, or every file from a
folder selected at once) -> match -> categorise -> download the claims
Excel + a folder of renamed, matched receipts.

Everything is local: files are uploaded directly, nothing is fetched from or
written back to any external service. Re-uploading the output anywhere
(e.g. SharePoint) is a manual, human step.
"""
from datetime import datetime

import streamlit as st
import pandas as pd
import anthropic

import config
from statement_parser import parse_bank_statement
from receipt_parser import parse_receipts
from matcher import match_receipts
from categorizer import categorize_transactions
from excel_handler import build_excel, build_zip, excel_filename, receipt_filename, myipo_reference_label


# ── Setup / guards ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Credit Card Claims Matcher", page_icon="💳", layout="wide")
st.title("💳 Credit Card Claims Matcher")
st.caption("Cross-check credit card statement transactions against receipts, and generate the claims Excel.")

if not config.ANTHROPIC_API_KEY:
    st.error("ANTHROPIC_API_KEY not set. Add it to `key.env` (see key.env.example), then restart the app.")
    st.stop()

if not config.BANKS:
    st.error("No banks configured. Add at least one entry to BANKS in config.py.")
    st.stop()

if not config.APP_PASSWORD:
    st.error(
        "APP_PASSWORD not set. Add it to Streamlit's Secrets (Community Cloud) or to `key.env` "
        "(local dev), then restart/redeploy the app."
    )
    st.stop()


def _check_password() -> bool:
    """Simple shared-password gate. Blocks the rest of the app until the correct
    password is entered; stays entered for the rest of the browser session."""
    if st.session_state.get("authenticated"):
        return True

    st.markdown("### 🔒 This app is password-protected")
    with st.form("password_form"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if entered == config.APP_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()


def show_api_error_and_stop(e: Exception):
    """Render a clean, actionable message for Anthropic API failures instead of a raw traceback."""
    msg = str(e)
    if isinstance(e, anthropic.APIStatusError) and "credit balance is too low" in msg.lower():
        st.error(
            "Your Anthropic account doesn't have enough credit balance to process requests.\n\n"
            "Go to console.anthropic.com → **Plans & Billing** to add credits or upgrade, then try again."
        )
    elif isinstance(e, anthropic.AuthenticationError):
        st.error(
            "Anthropic rejected the API key (authentication error). Double-check ANTHROPIC_API_KEY "
            "in key.env, then restart the app."
        )
    elif isinstance(e, anthropic.RateLimitError):
        st.error("Hit Anthropic's rate limit. Wait a bit and try again, or process fewer receipts at once.")
    elif isinstance(e, anthropic.APIStatusError):
        st.error(f"Anthropic API error ({e.status_code}): {msg}")
    else:
        st.error(f"Unexpected error while calling the Anthropic API: {msg}")
    st.stop()


st.divider()

# ── Inputs ───────────────────────────────────────────────────────────────────────

st.subheader("1. Choose Bank")
bank = st.selectbox("Which bank is this statement from?", config.BANKS)

st.subheader("2. Bank Statement")
statement_file = st.file_uploader("Credit card statement (PDF)", type="pdf", key="statement")

st.subheader("3. Receipts")
if "receipts_uploader_key" not in st.session_state:
    st.session_state.receipts_uploader_key = 0

receipt_files = st.file_uploader(
    "All receipt/invoice files for this statement (PDF, PNG, JPG)",
    type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True,
    key=f"receipts_{st.session_state.receipts_uploader_key}",
    help="Two ways to select a whole folder's worth at once: (1) click Browse, then in the file "
         "dialog select all files in the folder (Ctrl/Cmd+A, or Shift-click the range), or "
         "(2) open the folder in File Explorer/Finder and drag all the files into this box "
         "(most browsers support dragging a full multi-file selection this way). To remove a single "
         "file, use the × next to it in the list below - the button below removes all of them at once.",
)
if receipt_files:
    if st.button("🗑️ Remove all receipts", key="remove_all_receipts"):
        st.session_state.receipts_uploader_key += 1
        st.rerun()

# Status bar
parts = [
    "✅ Statement" if statement_file else "❌ Statement",
    f"✅ {len(receipt_files)} receipt(s)" if receipt_files else "❌ Receipts",
]
st.caption("  |  ".join(parts))
st.divider()

ready = bool(statement_file and receipt_files)
run_btn = st.button("Run Matching", type="primary", disabled=not ready, use_container_width=True)

if "result" not in st.session_state:
    st.session_state.result = None


# ── Run ──────────────────────────────────────────────────────────────────────────

if run_btn:
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    progress = st.progress(0, text="Starting...")

    try:
        # Step 1: statement
        progress.progress(10, text="Reading bank statement...")
        statement_bytes = statement_file.getvalue()
        try:
            data = parse_bank_statement(client, statement_bytes, bank_hint=bank)
        except anthropic.APIError as e:
            show_api_error_and_stop(e)

        transactions = []
        for i, tx in enumerate(data.get("transactions", []), start=1):
            transactions.append({
                "row_no": i,
                "posting_date": tx.get("posting_date", ""),
                "transaction_date": tx.get("transaction_date", ""),
                "description": tx.get("description", ""),
                "currency": tx.get("currency") or "MYR",
                "foreign_amount": tx.get("foreign_amount"),
                "amount_rm": round(float(tx.get("amount_rm") or 0), 2),
            })
        start_date = data.get("statement_start_date", "")
        end_date = data.get("statement_end_date", "")
        statement_month = (data.get("statement_month") or "").strip() or datetime.now().strftime("%B")
        statement_year = int(data.get("statement_year") or datetime.now().year)
        progress.progress(25, text=f"Found {len(transactions)} transaction(s). "
                                    f"Period: {start_date} - {end_date} ({statement_month} {statement_year}).")

        if not transactions:
            st.error("Could not extract any transactions from the statement. "
                      "Make sure it's a text-based (not scanned) or clearly legible PDF.")
            st.stop()

        # Step 2: read receipts (cached per exact file set so re-running without
        # changing anything doesn't burn Claude credits again)
        cache_key = ("upload", tuple(sorted((f.name, f.size) for f in receipt_files)))
        if st.session_state.get("receipt_cache_key") == cache_key:
            receipt_results = st.session_state["receipt_cache_results"]
            progress.progress(80, text=f"Using cached results for {len(receipt_files)} receipt(s). Matching...")
        else:
            placeholder = st.empty()
            file_dicts = [{"name": f.name, "content": f.getvalue()} for f in receipt_files]

            def upload_progress(i, total, name):
                pct = 30 + int((i / total) * 45)
                progress.progress(pct, text=f"Reading receipt {i}/{total}: {name}")
                placeholder.caption(f"Processing: {name}")

            try:
                receipt_results = parse_receipts(client, file_dicts, progress_callback=upload_progress)
            except anthropic.APIError as e:
                show_api_error_and_stop(e)
            placeholder.empty()
            st.session_state["receipt_cache_key"] = cache_key
            st.session_state["receipt_cache_results"] = receipt_results

        parsed_ok = [r for r in receipt_results if "error" not in r]
        parse_errors = [r for r in receipt_results if "error" in r]
        progress.progress(85, text=f"Parsed {len(parsed_ok)} receipt(s). Matching...")

        # Step 3: match
        transactions = match_receipts(transactions, receipt_results,
                                       statement_year=statement_year, statement_end_date=end_date)

        # Step 4: categorise
        progress.progress(92, text="Categorising transactions...")
        try:
            transactions = categorize_transactions(client, transactions)
        except anthropic.APIError as e:
            show_api_error_and_stop(e)

        # Step 5: rename matched receipts
        for t in transactions:
            r = t.get("matched_receipt")
            if r and "error" not in r:
                t["renamed_filename"] = receipt_filename(t, r["ext"], statement_year)

        # Step 6: build excel + zip
        progress.progress(97, text="Building Excel and ZIP...")
        excel_bytes = build_excel(
            transactions,
            bank=bank, month=statement_month, year=statement_year,
            start_date=start_date, end_date=end_date,
        )
        zip_bytes = build_zip(bank, statement_month, statement_year, start_date, end_date,
                               statement_bytes, excel_bytes, transactions)

        unmatched = [r for r in receipt_results if "error" not in r and not r.get("used")]

        progress.progress(100, text="Done.")

    except Exception as e:
        st.error(f"An error occurred: {e}")
        st.stop()

    st.session_state.result = {
        "bank": bank,
        "month": statement_month,
        "year": statement_year,
        "start_date": start_date,
        "end_date": end_date,
        "transactions": transactions,
        "excel_bytes": excel_bytes,
        "zip_bytes": zip_bytes,
        "unmatched": unmatched,
        "parse_errors": parse_errors,
        "checked_receipts": receipt_results,
    }


# ── Results ────────────────────────────────────────────────────────────────────

if st.session_state.result:
    res = st.session_state.result
    transactions = res["transactions"]

    st.success("Matching complete!", icon="✅")

    total = len(transactions)
    n_claimable = sum(1 for t in transactions if t.get("claimable"))
    n_matched = sum(1 for t in transactions if t.get("matched_receipt") and "error" not in t["matched_receipt"])

    m1, m2, m3 = st.columns(3)
    m1.metric("Transactions", total)
    m2.metric("Claimable", n_claimable)
    m3.metric("Matched to receipt", f"{n_matched}/{total}")

    if res["parse_errors"]:
        with st.expander(f"⚠️ {len(res['parse_errors'])} receipt(s) could not be parsed"):
            for err in res["parse_errors"]:
                st.write(f"• **{err['filename']}** — {err['error']}")

    st.divider()

    rows = []
    for t in transactions:
        r = t.get("matched_receipt")
        has_doc = bool(r) and bool(t.get("renamed_filename"))
        if has_doc and r.get("is_myipo"):
            doc_status = "MyIPO"
        elif has_doc:
            doc_status = "Yes"
        else:
            doc_status = "No"
        if has_doc:
            reference_value = t.get("renamed_filename", "")
        elif t.get("category") == "MyIPO":
            reference_value = myipo_reference_label(t, res["year"])
        else:
            reference_value = ""
        rows.append({
            "Posting Date": t["posting_date"],
            "Transaction Date": t["transaction_date"],
            "No.": t["row_no"],
            "Reference": reference_value,
            "Description": t["description"],
            "Currency": t["currency"],
            "Foreign Amount": t["foreign_amount"],
            "Amount (RM)": t["amount_rm"],
            "Category": t.get("category", ""),
            "Claim Notes": t.get("claim_notes", ""),
            "Doc?": doc_status,
        })
    df = pd.DataFrame(rows)

    def highlight(row, _transactions=transactions):
        t = _transactions[row.name]
        color = "#D9EBEF" if t.get("claimable") else "#FFF2CC"
        return [f"background-color: {color}"] * len(row)

    def negative_red(val):
        return "color: #C00000" if isinstance(val, (int, float)) and val < 0 else ""

    styled = (
        df.style
        .apply(highlight, axis=1)
        .format({"Foreign Amount": "{:.2f}", "Amount (RM)": "{:.2f}"}, na_rep="")
    )
    styler_negative = getattr(styled, "map", None) or getattr(styled, "applymap")
    styled = styler_negative(negative_red, subset=["Foreign Amount", "Amount (RM)"])
    st.dataframe(styled, use_container_width=True, height=500)

    if res["unmatched"]:
        st.warning(f"{len(res['unmatched'])} receipt(s) were available but did not match any transaction: "
                   + ", ".join(r["filename"] for r in res["unmatched"]))

    with st.expander("🔍 Debug: what was extracted from each receipt"):
        if res["checked_receipts"]:
            debug_rows = [{
                "File": r.get("filename", ""),
                "Extracted company": r.get("company_name", ""),
                "Extracted date": r.get("date", ""),
                "Extracted amount": r.get("amount"),
                "Currency": r.get("currency", ""),
                "Doc type": r.get("document_type", ""),
                "MyIPO?": r.get("is_myipo", False),
                "Matched?": "Yes" if r.get("used") else "No",
                "Error": r.get("error", ""),
            } for r in res["checked_receipts"]]
            st.dataframe(pd.DataFrame(debug_rows), use_container_width=True)
        else:
            st.caption("No receipts were available to check.")

    st.divider()
    st.download_button(
        "⬇️ Download Claims Excel only",
        data=res["excel_bytes"],
        file_name=excel_filename(res["month"], res["year"]),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    st.download_button(
        "⬇️ Download Excel + Matched Receipts (ZIP)",
        data=res["zip_bytes"],
        file_name=f"{res['bank']} - {res['month']} Claimable Invoice ({res['year']}).zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
    st.caption("Nothing is uploaded automatically - download the ZIP and upload the contents "
               "yourself once reviewed.")