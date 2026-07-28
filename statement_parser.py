"""
Claude-based credit card statement parser.
Works across banks since it reads the statement (text or, if scanned, page
images) rather than relying on one bank's specific line format.
"""
import re
import json
import base64
from io import BytesIO

import pdfplumber
import fitz  # PyMuPDF
import anthropic

MODEL = "claude-haiku-4-5-20251001"

STATEMENT_SYSTEM_PROMPT = """You are a meticulous credit card statement reader for a Malaysian company's finance admin team.
{bank_hint}
You will be given the content of a credit card statement (either as text or as page images).
Extract EVERY transaction line (purchases, fees, refunds, interest charges) - do not skip anything, and do not include
subtotals, running balances, minimum payment info, or marketing text.

Respond with ONLY valid JSON (no markdown fences, no commentary) in this exact shape:
{{
  "statement_month": "the single calendar month name this statement's claim period is filed under, e.g. May - use the month of the statement's STARTING/opening date (not the closing date), matching how these statements are conventionally named - e.g. a period of 15 June - 13 July is filed under June",
  "statement_year": <number> (the calendar year of the statement's STARTING/opening date, e.g. 2026, as printed on the statement or inferred if not shown),
  "statement_start_date": "D Month" (e.g. "8 May"),
  "statement_end_date": "D Month" (e.g. "5 June"),
  "transactions": [
    {{
      "posting_date": "as printed on the statement, e.g. 12 May",
      "transaction_date": "as printed on the statement, e.g. 10 May",
      "description": "the raw merchant / description text exactly as printed",
      "currency": "3-letter code of the currency the merchant charged in, e.g. USD, SGD, MYR",
      "foreign_amount": <number or null> (the amount in the foreign currency column, null if the transaction was already in MYR),
      "amount_rm": <number> (the RM amount actually charged to the card / statement balance; use a NEGATIVE number for credits/refunds/payments received)
    }}
  ]
}}
If the statement shows dates without a year, keep them as printed (no year). Numbers must be plain numbers (no "RM", no commas).
"""


def _parse_json_response(text: str):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def _image_block(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode(),
        },
    }


def extract_pdf_text(file_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts)


def pdf_to_page_images(file_bytes: bytes, dpi: int = 150, max_pages: int = 15) -> list[bytes]:
    images = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=mat)
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def parse_bank_statement(client: anthropic.Anthropic, pdf_bytes: bytes, bank_hint: str = "") -> dict:
    """
    Returns:
      {
        "statement_month": str, "statement_year": int,
        "statement_start_date": str, "statement_end_date": str,
        "transactions": [{"posting_date", "transaction_date", "description",
                           "currency", "foreign_amount", "amount_rm"}, ...]
      }
    """
    text = extract_pdf_text(pdf_bytes)
    images = None
    if len(text.strip()) < 200:
        images = pdf_to_page_images(pdf_bytes)
        text = None

    content = []
    if images:
        content.append({"type": "text", "text": "Here are the statement pages as images. Extract every transaction."})
        for img in images:
            content.append(_image_block(img))
    else:
        content.append({"type": "text", "text": f"Here is the statement text:\n\n{text}"})

    hint = f"This statement is from {bank_hint}." if bank_hint else ""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=STATEMENT_SYSTEM_PROMPT.format(bank_hint=hint),
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    return _parse_json_response(raw)