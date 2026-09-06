"""Local OCR for photographed Moldovan fiscal invoices.

OCR creates a draft only when identity, parties, every item and totals reconcile.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime
from itertools import pairwise

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
		"\u0423": "Y",
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


def _party_pair(block: str, pairs: list[tuple[str, str]]) -> tuple[str | None, str | None]:
	direct = _id_vat(block)
	if direct[0] and direct[0].startswith("1"):
		return direct
	vat_ids = re.findall(r"\b\d{7}\b", block)
	return next((pair for pair in pairs if pair[0].startswith("1") and pair[1] in vat_ids), (None, None))


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


def _read_cell(gray, top, bottom, left, right, *, numbers=False) -> str:
	import cv2
	import pytesseract

	crop = gray[top + 2 : bottom - 2, left + 2 : right - 2]
	if crop.size == 0:
		return ""
	crop = cv2.resize(crop, None, fx=2.5, fy=2.5)
	config = "--psm 7"
	if numbers:
		config += " -c tessedit_char_whitelist=0123456789.,-"
	return pytesseract.image_to_string(crop, lang="eng" if numbers else "ron+eng", config=config).strip()


def _clusters(values: list[int], tolerance=5) -> list[int]:
	groups: list[list[int]] = []
	for value in sorted(values):
		if groups and value - groups[-1][-1] <= tolerance:
			groups[-1].append(value)
		else:
			groups.append([value])
	return [round(sum(group) / len(group)) for group in groups]


def _amount_cell(value: str):
	match = _DECIMAL.search(value)
	return decimal(_number(match[1])) if match else None


def _rate_and_qty(row_text: str, rate_text: str, net):
	# Prefer the dedicated rate cell. Wider row OCR is only a fallback because it also contains net/total.
	values = [_number(value) for value in _DECIMAL.findall(rate_text + " " + row_text)]
	rates = []
	for value in values:
		candidate = decimal(value)
		rates.append(candidate)
		# OCR sometimes prefixes a grid fragment to an otherwise correct rate.
		if len(value.split(".", 1)[0]) >= 5:
			rates.append(decimal(value[1:]))
	qtys = [decimal(str(i / 10)) for i in range(1, 101)] + [decimal(str(i)) for i in range(11, 101)]
	for rate in rates:
		matches = [qty for qty in qtys if money(qty * rate) == money(net)]
		if matches:
			matches.sort(key=lambda qty: (qty % 1 != 0, qty))
			return matches[0], rate
	return decimal("1"), net


def _grid_items(gray) -> tuple[list[dict], tuple | None]:
	"""Read each ruled table row independently; whole-page OCR merges adjacent columns."""
	import cv2

	height, width = gray.shape[:2]
	threshold = cv2.threshold(gray, 190, 255, cv2.THRESH_BINARY_INV)[1]
	horizontal = cv2.morphologyEx(
		threshold, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(400, width // 6), 1))
	)
	contours, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
	horizontal_boxes = [cv2.boundingRect(contour) for contour in contours]
	y_values = _clusters(
		[y for x, y, w, h in horizontal_boxes if w > width * 0.75 and h < 20 and y > height * 0.25]
	)
	sequences = []
	current = []
	for y in y_values:
		if current and not 25 <= y - current[-1] <= 90:
			if len(current) >= 4:
				sequences.append(current)
			current = []
		current.append(y)
	if len(current) >= 4:
		sequences.append(current)
	if not sequences:
		return [], None
	y_lines = max(sequences, key=len)
	row_span = y_lines[-1] - y_lines[0]
	vertical = cv2.morphologyEx(
		threshold, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(300, row_span // 3)))
	)
	contours, _ = cv2.findContours(vertical, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
	x_values = _clusters(
		[x for contour in contours for x, y, w, h in [cv2.boundingRect(contour)] if h > row_span * 0.7]
	)
	starts = [x for x, y, w, h in horizontal_boxes if y_lines[0] - 10 <= y <= y_lines[-1] + 10]
	if not starts:
		return [], None
	left = min((x for x in starts if x > width * 0.02), default=min(starts))
	x_lines = [left, *[x for x in x_values if x > left + 100]]
	if len(x_lines) < 9:
		return [], None
	x_lines = x_lines[:9]
	items = []
	totals = None
	for top, bottom in pairwise(y_lines):
		name = _read_cell(gray, top, bottom, x_lines[0], x_lines[1]).strip(" |._-")
		if "total" in name.lower() and re.search(r"pagina|factura", name, re.I):
			net = _amount_cell(_read_cell(gray, top, bottom, x_lines[4], x_lines[5], numbers=True))
			vat = _amount_cell(_read_cell(gray, top, bottom, x_lines[6], x_lines[7], numbers=True))
			gross = _amount_cell(_read_cell(gray, top, bottom, x_lines[7], x_lines[8], numbers=True))
			if net is not None and vat is not None and gross is not None:
				totals = tuple(map(money, (net, vat, gross)))
			continue
		if len(name) < 3:
			continue
		row_text = _read_cell(gray, top, bottom, x_lines[0], x_lines[8])
		rate_text = _read_cell(gray, top, bottom, x_lines[3], x_lines[4], numbers=True)
		net = _amount_cell(_read_cell(gray, top, bottom, x_lines[4], x_lines[5], numbers=True))
		vat = _amount_cell(_read_cell(gray, top, bottom, x_lines[6], x_lines[7], numbers=True))
		gross = _amount_cell(_read_cell(gray, top, bottom, x_lines[7], x_lines[8], numbers=True))
		if None in (net, vat, gross) or money(net + vat) != money(gross):
			continue
		qty, rate = _rate_and_qty(row_text, rate_text, net)
		if qty is None:
			continue
		vat_rate = money(vat * 100 / net) if net else decimal("0")
		if vat_rate not in (decimal("0.00"), decimal("5.00"), decimal("8.00"), decimal("20.00")):
			continue
		items.append(
			{
				"description": name,
				"source_uom": "buc",
				"source_qty": str(qty),
				"source_rate": str(rate),
				"net_amount": str(net),
				"vat_rate": str(vat_rate),
				"vat_amount": str(vat),
				"amount": str(gross),
				"qty": str(qty),
			}
		)
	return items, totals


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
	supplier_pattern = (r"1\.\s*Fur(?:n|m)izor", r"2\.\s*(?:Cumparator|Cumpărător)")
	customer_pattern = (r"2\.\s*(?:Cumparator|Cumpărător)(?:/beneficiar)?", r"3\.\s*Deleg")
	supplier_blocks = [_party(source, *supplier_pattern) for source in (raw_text, clean_text)]
	customer_blocks = [_party(source, *customer_pattern) for source in (raw_text, clean_text)]
	supplier_block = next((block for block in reversed(supplier_blocks) if block), "")
	customer_block = next((block for block in reversed(customer_blocks) if block), "")
	if not identity or not issue_date or len(pairs) < 2 or not supplier_block or not customer_block:
		_fail()
	items, grid_totals = _grid_items(gray)
	if not items:
		items = _items(clean_text)
	if not items:
		_fail()
	sums = (money(sum(decimal(row[key]) for row in items)) for key in ("net_amount", "vat_amount", "amount"))
	calculated = tuple(sums)
	candidates = ([grid_totals] if grid_totals else []) + _totals(raw_text, clean_text)
	matched_totals = next((candidate for candidate in candidates if candidate == calculated), None)
	if not matched_totals:
		_fail()
	valid_pairs = list(dict.fromkeys(pair for pair in pairs if pair[0].startswith("1")))
	vat_order = list(dict.fromkeys(pair[1] for pair in pairs))
	if len(vat_order) < 2:
		_fail()
	supplier_pair = next((pair for pair in valid_pairs if pair[1] == vat_order[0]), (None, None))
	customer_pair = next((pair for pair in valid_pairs if pair[1] == vat_order[1]), (None, None))
	if None in (*supplier_pair, *customer_pair):
		_fail()
	supplier_idno, supplier_vat = supplier_pair
	customer_idno, customer_vat = customer_pair
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
