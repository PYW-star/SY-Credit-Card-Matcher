"""
Match credit card statement transactions to receipts.

A candidate match must satisfy BOTH:
  - amount: exact match (a tiny epsilon only accounts for floating-point
    rounding, not a real tolerance) against either the transaction's foreign
    amount or its RM amount
  - date: if both the transaction and the receipt have a parseable date,
    they must fall within a small window of each other (receipts are often
    dated the actual purchase day, which can differ slightly from the
    statement's posting/transaction date) - if either date can't be parsed,
    this check is skipped rather than blocking a real match over a read error

Merchant/company name similarity is then used only to rank and break ties
among whatever candidates pass both of the above.
"""
import re
import difflib
from datetime import date

_EPSILON = 0.005  # floating-point safety margin only, not a real amount tolerance
_DATE_WINDOW_DAYS = 5  # how far apart a transaction date and receipt date may be and still count

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _month_from_name(name: str) -> int | None:
    return _MONTH_MAP.get((name or "").strip().lower()[:3])


def _parse_statement_date(date_str: str, statement_year: int | None, end_month: int | None) -> date | None:
    """Parse a 'D Month' string (e.g. '10 May') into a real date, using statement_year -
    or statement_year - 1 if this transaction's month is later in the calendar than the
    statement's closing month (handles a statement period that crosses a year boundary,
    e.g. 28 December - 27 January)."""
    if not date_str or not statement_year:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)", date_str)
    if not m:
        return None
    day = int(m.group(1))
    month = _month_from_name(m.group(2))
    if not month:
        return None
    year = statement_year
    if end_month and month > end_month:
        year = statement_year - 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_receipt_date(ddmmyy: str) -> date | None:
    """Parse a 6-digit DDMMYY string (as produced by receipt_parser) into a real date."""
    if not ddmmyy or not re.fullmatch(r"\d{6}", ddmmyy):
        return None
    try:
        day = int(ddmmyy[0:2])
        month = int(ddmmyy[2:4])
        year = 2000 + int(ddmmyy[4:6])
        return date(year, month, day)
    except ValueError:
        return None


def match_receipts(transactions: list[dict], receipts: list[dict],
                    statement_year: int | None = None, statement_end_date: str = "") -> list[dict]:
    """
    transactions: list of dicts with "foreign_amount" (float|None), "amount_rm" (float),
                  "description" (str), and ideally "transaction_date"/"posting_date"
                  ('D Month' strings). Mutated in place - each gets "matched_receipt".
    receipts: list of dicts with "amount" (float), "company_name" (str), "date" (DDMMYY
              string). Mutated in place - each gets a "used" key (bool).
    statement_year / statement_end_date: used to resolve transaction dates to real
              calendar dates for comparison against receipt dates. If omitted, date
              proximity is skipped entirely and matching falls back to amount + name only.
    Returns the same transactions list for convenience.
    """
    for r in receipts:
        r.setdefault("used", False)
    for t in transactions:
        t.setdefault("matched_receipt", None)

    end_month = None
    if statement_end_date:
        m = re.search(r"([A-Za-z]+)\s*$", statement_end_date.strip())
        if m:
            end_month = _month_from_name(m.group(1))

    candidates = []
    for ti, t in enumerate(transactions):
        t_amount = t["foreign_amount"] if t.get("foreign_amount") is not None else t["amount_rm"]
        t_amount = abs(t_amount)
        t_date = _parse_statement_date(
            t.get("transaction_date") or t.get("posting_date") or "", statement_year, end_month
        )

        for ri, r in enumerate(receipts):
            if r.get("used") or "error" in r:
                continue

            diff_foreign = abs(r["amount"] - t_amount)
            diff_rm = abs(r["amount"] - abs(t["amount_rm"]))
            if diff_foreign > _EPSILON and diff_rm > _EPSILON:
                continue  # amount doesn't match at all - never a candidate

            r_date = _parse_receipt_date(r.get("date", ""))
            date_score = 0.5  # neutral when either date is unavailable/unparseable
            if t_date and r_date:
                diff_days = abs((t_date - r_date).days)
                if diff_days > _DATE_WINDOW_DAYS:
                    continue  # amount matched but dates are too far apart - not this one
                date_score = 1.0 - (diff_days / (_DATE_WINDOW_DAYS + 1))

            name_score = _name_similarity(r.get("company_name", ""), t.get("description", ""))
            score = date_score * 0.6 + name_score * 0.4
            candidates.append((score, ti, ri))

    candidates.sort(key=lambda x: -x[0])
    matched_t, matched_r = set(), set()
    for score, ti, ri in candidates:
        if ti in matched_t or ri in matched_r:
            continue
        transactions[ti]["matched_receipt"] = receipts[ri]
        receipts[ri]["used"] = True
        matched_t.add(ti)
        matched_r.add(ri)

    return transactions