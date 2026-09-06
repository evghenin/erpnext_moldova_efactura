"""Local OCR for photographed Moldovan fiscal invoices.

OCR creates a draft only when identity, parties, every item and totals reconcile.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
OCR_ERROR = (
	"Unable to obtain complete and consistent factura data. "
	"Please provide a clearer, correctly oriented photo or scan of the complete document."
)
_DECIMAL = re.compile(r"(?<!\d)(\d{1,7}[.,:\-]\d{2})(?!\d)")
_CYRILLIC_LATIN = str.maketrans(
	{
		"\u0410": "A",
		"\u0412": "B",
		"\u0415": "E",
		"\u041a": "K",
		"\u041c": "M",
		"\u041d": "H",
		"\u041e": "O",
		"\u0420": "P",
		"\u0421": "C",
		"\u0422": "T",
		"\u0425": "X",
	}
)


def _fail():
	raise FacturaImportError(OCR_ERROR)


def _number(value: str) -> str:
	return value.replace(" ", "").replace(",", ".").replace(":", ".").replace("-", ".")


def _date(text: str) -> str | None:
	match = re.search(r"\b(\d{2})[.\-/](\d{2})[.\-/](20\d{2})\b", text)
	if match:
		return f"{match[3]}-{match[2]}-{match[1]}"
	months = {
		"ianuarie": 1,
		"februarie": 2,
		"martie": 3,
		"aprilie": 4,
		"mai": 5,
		"iunie": 6,
		"iulie": 7,
		"august": 8,
		"septembrie": 9,
		"octombrie": 10,
		"noiembrie": 11,
		"decembrie": 12,
	}
	match = re.search(r"\b(\d{1,2})\s+([A-Za-zăâîșț]+)\s+(20\d{2})\b", text, re.I)
	if match and match[2].lower() in months:
		return datetime(int(match[3]), months[match[2].lower()], int(match[1])).date().isoformat()
	return None


def _prepare(content: bytes):
	try:
		import cv2
		import numpy as np
	except ImportError:
		_fail()
	if len(content) > MAX_IMAGE_BYTES or not (
		content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")
	):
		_fail()
	image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
	if image is None or image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
		_fail()
	if image.shape[1] > image.shape[0]:
		image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
	gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
	gray = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8)).apply(gray)
	binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15)
	inverse = 255 - binary
	horizontal = cv2.morphologyEx(inverse, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
	vertical = cv2.morphologyEx(inverse, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 60)))
	without_grid = cv2.bitwise_or(binary, cv2.bitwise_or(horizontal, vertical))
	return gray, without_grid


def _ocr(image, psm: int) -> str:
	if not shutil.which("tesseract"):
		_fail()
	try:
		import pytesseract

		return pytesseract.image_to_string(
			image, lang="ron+rus+eng", config=f"--psm {psm} -c preserve_interword_spaces=1", timeout=45
		)
	except Exception:
		_fail()


def _party(text: str, start: str, end: str) -> str:
	match = re.search(start + r"\s*:?\s*(.+?)(?=" + end + ")", text, re.I | re.S)
	return " ".join(match[1].split()) if match else ""


def _iban(block: str) -> str | None:
	latin = block.upper().translate(_CYRILLIC_LATIN)
	match = re.search(r"\bMD\s*\d{2}[A-Z0-9\s\\/]{16,28}", latin)
	if not match:
		return None
	value = re.sub(r"[^A-Z0-9]", "", match[0])
	return value[:24] if len(value) >= 24 else None


def _id_vat(block: str) -> tuple[str | None, str | None]:
	pairs = re.findall(r"\b(\d{13})\s*/\s*(\d{7})\b", block)
	return pairs[0] if pairs else (None, None)


def _items(text: str) -> list[dict]:
	items = []
	for line in text.splitlines():
		uom = re.search(r"\b(buc|serv)\.?\b", line, re.I)
		if not uom:
			continue
		name = re.sub(r"^[|\s._-]+", "", line[: uom.start()]).strip(" |._-")
		if len(name) < 3 or "unitate" in name.lower():
			continue
		tail = line[uom.end() :]
		qty_match = re.search(r"\b(\d+(?:[.,]\d+)?)\b", tail)
		values = [_number(v) for v in _DECIMAL.findall(tail)]
		if not qty_match or len(values) < 5:
			continue
		qty = decimal(qty_match[1])
		rate, net, vat_rate, vat, gross = map(decimal, values[-5:])
		if vat_rate not in (decimal("0"), decimal("5"), decimal("8"), decimal("20")):
			continue
		if money(qty * rate) != money(net) or money(net * vat_rate / 100) != money(vat):
			continue
		if money(net + vat) != money(gross):
			continue
		items.append(
			{
				"description": name,
				"source_uom": uom[1].lower(),
				"source_qty": str(qty),
				"source_rate": str(rate),
				"net_amount": str(net),
				"vat_rate": str(vat_rate),
				"vat_amount": str(vat),
				"amount": str(gross),
				"qty": str(qty),
			}
		)
	return items


def _totals(*texts: str) -> list[tuple]:
	results = []
	for text in texts:
		for line in text.splitlines():
			if not re.search(r"12[.,]?\s*total", line, re.I):
				continue
			values = [decimal(_number(v)) for v in _DECIMAL.findall(line)]
			if len(values) >= 3:
				results.append(tuple(map(money, values[-3:])))
	return results


def parse_image(content: bytes) -> dict:
	gray, clean = _prepare(content)
	raw_text, clean_text = _ocr(gray, 3), _ocr(clean, 6)
	text = raw_text + "\n" + clean_text
	header = text.upper().translate(_CYRILLIC_LATIN)
	identity = re.search(r"\b(A{2}[A-Z])\s*(\d{7})\b", header)
	issue_date = _date(text)
	pairs = re.findall(r"\b(\d{13})\s*/\s*(\d{7})\b", text)
	supplier_block = _party(text, r"1\.\s*Furnizor", r"2\.\s*(?:Cumparator|Cumpărător)")
	customer_block = _party(text, r"2\.\s*(?:Cumparator|Cumpărător)(?:/beneficiar)?", r"3\.\s*Deleg")
	if not identity or not issue_date or len(pairs) < 2 or not supplier_block or not customer_block:
		_fail()
	items = _items(clean_text)
	if not items:
		_fail()
	sums = (money(sum(decimal(row[key]) for row in items)) for key in ("net_amount", "vat_amount", "amount"))
	calculated = tuple(sums)
	matched_totals = next(
		(candidate for candidate in _totals(raw_text, clean_text) if candidate == calculated), None
	)
	if not matched_totals:
		_fail()
	supplier_idno, supplier_vat = _id_vat(supplier_block)
	customer_idno, customer_vat = _id_vat(customer_block)
	if (supplier_idno, supplier_vat, customer_idno, customer_vat) != (*pairs[0], *pairs[1]):
		_fail()
	return {
		"provider": "OCR",
		"series": identity[1],
		"number": identity[2],
		"issue_date": issue_date,
		"delivery_date": issue_date,
		"supplier_name": supplier_block.split(" c.f", 1)[0].strip(),
		"supplier_idno": supplier_idno,
		"supplier_vat_id": supplier_vat,
		"supplier_address": supplier_block,
		"supplier_bank_account": _iban(supplier_block),
		"buyer_name": customer_block.split(" c.f", 1)[0].strip(),
		"buyer_idno": customer_idno,
		"buyer_vat_id": customer_vat,
		"buyer_address": customer_block,
		"buyer_bank_account": _iban(customer_block),
		"currency": "MDL",
		"net_total": str(matched_totals[0]),
		"vat_total": str(matched_totals[1]),
		"total": str(matched_totals[2]),
		"items": items,
		"original_format": "Paper",
		"signature_status": "Not Applicable",
		"signature_details": [],
		"file_hash": hashlib.sha256(content).hexdigest(),
		"extracted_text": raw_text,
	}
