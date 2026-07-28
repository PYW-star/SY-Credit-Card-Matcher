"""
Parse receipts/invoices (PDF or image) into structured fields via Claude.
Runs extraction concurrently across receipts since each is an independent call.
Works on raw bytes so it doesn't matter how the file was obtained -
uploaded directly by the user in every case for this app.
"""
import re
import json
import base64
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import pdfplumber
import fitz  # PyMuPDF
import anthropic

MODEL = "claude-haiku-4-5-20251001"

RECEIPT_SYSTEM_PROMPT = """You are reading a scanned receipt or invoice for a Malaysian IP firm's expense claims process.
Look at the document (image or text) and extract details. Pay special attention to whether this is a MyIPO
(Malaysia Intellectual Property Office / Companies Commission / other government IP-related) filing receipt -
these will usually show an "Application No.", "Application Number", trademark/patent/industrial design filing
reference, or be issued by MyIPO / IPOS / a government IP registry.

Respond with ONLY valid JSON (no markdown fences, no commentary) in this exact shape:
{
  "company_name": "the merchant / vendor / issuing company or authority, short form, no punctuation issues",
  "document_type": "Receipt" or "INV" (use INV if it is an invoice/tax invoice, Receipt otherwise),
  "date": "DDMMYY (day, month, 2-digit year - 6 digits total, e.g. 110526 for 11 May 2026) if a date is visible, else empty string",
  "amount": <number> (the total amount paid, plain number),
  "currency": "3-letter currency code, default MYR if not shown",
  "reference_no": "invoice/receipt/order number if visible, else empty string",
  "application_number": "the trademark/patent/design application number if this is a MyIPO-related document, else empty string",
  "is_myipo": true or false
}
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


def _extract_pdf_text(file_bytes: bytes) -> str:
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _pdf_to_page_images(file_bytes: bytes, max_pages: int = 3) -> list[bytes]:
    images = []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    mat = fitz.Matrix(150 / 72, 150 / 72)
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        images.append(page.get_pixmap(matrix=mat).tobytes("png"))
    doc.close()
    return images


def parse_receipts(client: anthropic.Anthropic, files: list[dict], progress_callback=None) -> list[dict]:
    """
    files: list of {"name": str, "content": bytes}
    Returns a list of dicts, one per file, in the same order, each either:
      {"company_name", "document_type", "date", "amount", "currency",
       "reference_no", "application_number", "is_myipo", "filename", "ext", "content"}
    or on failure: {"error": str, "filename": str}
    """
    results = [None] * len(files)
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_idx = {
            executor.submit(_parse_one, client, f): i for i, f in enumerate(files)
        }
        for completed, future in enumerate(as_completed(future_to_idx), 1):
            i = future_to_idx[future]
            results[i] = future.result()
            if progress_callback:
                progress_callback(completed, len(files), files[i]["name"])
    return results


def _parse_one(client: anthropic.Anthropic, f: dict) -> dict:
    name = f["name"]
    content_bytes = f["content"]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ("pdf", "png", "jpg", "jpeg"):
        return {"error": f"Unsupported file type: .{ext}", "filename": name}

    try:
        content = []
        if ext == "pdf":
            text = _extract_pdf_text(content_bytes)
            if len(text.strip()) >= 40:
                content.append({"type": "text", "text": f"Receipt text:\n\n{text}"})
            else:
                content.append({"type": "text", "text": "Receipt page image(s):"})
                for img in _pdf_to_page_images(content_bytes):
                    content.append(_image_block(img))
        else:
            content.append({"type": "text", "text": "Receipt image:"})
            content.append(_image_block(content_bytes))

        resp = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            system=RECEIPT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        data = _parse_json_response(raw)

        return {
            "company_name": data.get("company_name", ""),
            "document_type": data.get("document_type", "Receipt"),
            "date": data.get("date", ""),
            "amount": float(data.get("amount") or 0),
            "currency": data.get("currency") or "MYR",
            "reference_no": data.get("reference_no", ""),
            "application_number": data.get("application_number", ""),
            "is_myipo": bool(data.get("is_myipo")),
            "filename": name,
            "ext": ext,
            "content": content_bytes,
        }
    except Exception as e:
        return {"error": str(e), "filename": name}