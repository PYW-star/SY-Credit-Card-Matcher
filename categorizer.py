"""
Assign a category and claim status to every transaction via Claude.
"""
import json

import anthropic

MODEL = "claude-haiku-4-5-20251001"

CLAIMABLE_CATEGORIES = [
    "MyIPO",
    "Open AI",
    "Claude",
    "Microsoft",
    "Travel Air Ticket",
    "Software Subscriptions",
    "Gov/Regulatory Fees",
    "Utility/Admin",
]
NON_CLAIMABLE_CATEGORIES = ["F&B", "Normal Payment", "Credit Refund", "Bank Interest"]
ALL_CATEGORIES = CLAIMABLE_CATEGORIES + NON_CLAIMABLE_CATEGORIES

CATEGORY_SYSTEM_PROMPT = f"""You are categorising credit card transactions for a Malaysian IP firm's expense claim.

Valid categories are ONLY:
Claimable: {", ".join(CLAIMABLE_CATEGORIES)}
Non-claimable: {", ".join(NON_CLAIMABLE_CATEGORIES)}

Rules:
- If matched_receipt.is_myipo is true, OR an application_number is present, OR the transaction
  description itself clearly indicates a MyIPO / government IP registry payment (e.g. contains
  "MYIPO", "IPOS", "Intellectual Property Corporation", or similar) -> category "MyIPO", claimable true.
  This applies even when there is no matched receipt - recognise it from the description alone.
- OpenAI / ChatGPT charges -> "Open AI", claimable true.
- Anthropic / Claude charges -> "Claude", claimable true.
- Microsoft / Office 365 / Azure charges -> "Microsoft", claimable true.
- Airlines, travel agents, flight bookings -> "Travel Air Ticket", claimable true.
- Recognised software/SaaS subscriptions (Adobe, Zoom, Canva, Notion, Google Workspace, Dropbox, Grammarly, Github, etc.) -> "Software Subscriptions", claimable true.
- Statutory / government body fees not related to IP filings (e.g. SSM, LHDN, licenses, permits) -> "Gov/Regulatory Fees", claimable true.
- Utility bills or general office admin charges (electricity, water, internet, postage, courier, office supplies) -> "Utility/Admin", claimable true.
- Restaurants, cafes, food delivery -> "F&B", claimable false.
- Retail purchases / normal shopping unrelated to business -> "Normal Payment", claimable false.
- Refunds / credits appearing on the statement -> "Credit Refund", claimable false.
- Bank interest / finance charges -> "Bank Interest", claimable false.

For claim_notes:
- If category is "Gov/Regulatory Fees" or "Utility/Admin", claim_notes must be exactly "Fully claimable".
- Otherwise write a short note (under 12 words) describing what the transaction was for and its claim status,
  e.g. "OpenAI ChatGPT Plus subscription - claimable" or "Dinner with client - not claimable".

Respond with ONLY valid JSON: a list, same length and order as the input, of objects:
{{"category": "...", "claimable": true/false, "claim_notes": "..."}}
"""


def _parse_json_response(text: str):
    import re
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def categorize_transactions(client: anthropic.Anthropic, transactions: list[dict]) -> list[dict]:
    """Mutates each transaction dict with "category", "claimable", "claim_notes"."""
    items = []
    for t in transactions:
        entry = {"description": t.get("description", ""), "amount_rm": t.get("amount_rm"), "currency": t.get("currency")}
        r = t.get("matched_receipt")
        if r:
            entry["matched_receipt"] = {
                "company_name": r.get("company_name", ""),
                "is_myipo": r.get("is_myipo", False),
                "application_number": r.get("application_number", ""),
            }
        items.append(entry)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=CATEGORY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(items, ensure_ascii=False)}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    results = _parse_json_response(raw)
    for t, r in zip(transactions, results):
        cat = r.get("category", "")
        t["category"] = cat if cat in ALL_CATEGORIES else "Normal Payment"
        t["claimable"] = bool(r.get("claimable"))
        notes = r.get("claim_notes", "")
        if t["category"] in ("Gov/Regulatory Fees", "Utility/Admin"):
            notes = "Fully claimable"
        t["claim_notes"] = notes
    return transactions