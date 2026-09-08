"""Local OCR for photographed Moldovan fiscal invoices.

OCR creates a draft only when identity, parties, every item and totals reconcile.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import datetime
from importlib import import_module
from itertools import pairwise

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
OCR_ERROR = "Cannot read factura"
OCR_GUIDANCE = "Please provide a clearer, correctly oriented photo or scan of the complete document."
_DECIMAL = re.compile(r"(?<!\d)(\d{1,3}(?:[ ,]\d{3})+[.,:\-]\d{2}|\d{1,7}[.,:\-]\d{2})(?!\d)")
_AMOUNT = r"(\d{1,3}(?:[ ,]\d{3})+[.,:\-]\d{2}|\d{1,7}[.,:\-]\d{2})"
_RETAIL_TOKEN = re.compile(
	r"(?<!\d)(\d{1,3}(?:[ ,]\d{3})+[.,:\-]\d{2}|\d{1,7}[.,:\-]\d{2}|\d{1,4}(?!\d))"
)
_IDENTITY = re.compile(r"\b(A{2}[A-Z])(?:\s*NR\.?)?\s*(\d{7})\b")
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


def _fail(reason: str):
	raise FacturaImportError(f"{OCR_ERROR}: {reason}. {OCR_GUIDANCE}")


def _number(value: str) -> str:
	value = value.replace(" ", "")
	if "," in value and "." in value:
		value = value.replace(",", "")
	return value.replace(",", ".").replace(":", ".").replace("-", ".")


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


def _missing_image_dependencies() -> list[str]:
	missing = []
	for package, module in (
		("opencv-python-headless", "cv2"),
		("numpy", "numpy"),
		("pytesseract", "pytesseract"),
	):
		try:
			import_module(module)
		except Exception:
			missing.append(package)
	return missing


def _prepare(content: bytes):
	if len(content) > MAX_IMAGE_BYTES or not (
		content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")
	):
		_fail("the source is not a supported JPEG or PNG image, or it exceeds 15 MB")
	missing = _missing_image_dependencies()
	if missing:
		_fail("required Python libraries are missing: " + ", ".join(missing))
	import cv2
	import numpy as np
	image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
	if image is None or image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
		_fail("the image is damaged or exceeds 30 megapixels")
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
		_fail("the Tesseract OCR engine is not installed on the server")
	try:
		import pytesseract

		return pytesseract.image_to_string(
			image, lang="ron+eng", config=f"--psm {psm} -c preserve_interword_spaces=1", timeout=45
		)
	except Exception:
		_fail("the OCR engine could not process the image")


def _party(text: str, start: str, end: str) -> str:
	match = re.search(start + r"\s*:?\s*(.+?)(?=" + end + ")", text, re.I | re.S)
	return " ".join(match[1].split()).lstrip(" .:;|=-").rstrip(" :;|=-") if match else ""


def _party_name(block: str) -> str:
	block = " ".join(block.split())
	block = re.sub(r"^[iIl1]\s+(?=[A-ZĂÂÎȘȚ\"])", "", block)
	prefix = re.match(r"((?:S\.?R\.?L\.?|S\.?A\.?)\s+['‘\"].+?['’\"])(?=\s|[,;]|$)", block, re.I)
	if prefix:
		return prefix[1].strip(" ,;")
	match = re.match(r"(.+?\b(?:S\.?R\.?L\.?|S\.?A\.?))(?=\s|[,;]|$)", block, re.I)
	if match:
		return match[1].strip(" ,;")
	match = re.split(
		r"\s*(?:,\s*(?:R\.?M\.?|MD-?\d{4}|mun\.?|str\.?|c/d|c\.f\.?|nr\.?TVA)|\s+\d{13}/\d{7})",
		block,
		maxsplit=1,
		flags=re.I,
	)
	return match[0].strip(" ,;")


def _party_address(block: str) -> str:
	block = " ".join(block.split())
	block = re.sub(r"^[iIl1]\s+(?=[A-ZĂÂÎȘȚ\"])", "", block)
	name = _party_name(block)
	address = block[len(name) :].lstrip(" ,;") if block.startswith(name) else block
	address = re.sub(r"^['‘\"].+?['’\"]\s*", "", address)
	address = re.split(
		r"\s*(?:IBAN\b|\bin\s+BC\b|BIC\b|c\s*/\s*d|c\.\s*f\.?|e?f\s*/\s*nr|f\s*/\s*ar|nr\.?\s*TVA|\d{13}\s*/\s*\d{7})",
		address,
		maxsplit=1,
		flags=re.I,
	)[0]
	return address.strip(" \t,;:|-=_'\"‘’“”«»")


def _iban(block: str) -> str | None:
	latin = block.upper().translate(_CYRILLIC_LATIN)
	match = re.search(r"\bMD\s*\d{2}[A-Z0-9\s\\/]{16,28}", latin)
	if not match:
		return None
	value = re.sub(r"[^A-Z0-9]", "", match[0])
	return value[:24] if len(value) >= 24 else None


def _bank_details(block: str) -> tuple[str | None, str | None]:
	latin = " ".join(block.upper().translate(_CYRILLIC_LATIN).split())
	code_match = re.search(r"\b[A-Z]{4}MD[A-Z0-9]{2,7}\b", latin)
	code = code_match[0] if code_match else None
	bank_match = re.search(
		r"\bB\.?C\.?\s*(.+?)(?=\s*(?:,|BIC\b|O\.?F\.?\s*/\s*NR\.?\s*TVA\b|КОД\s*НДС\b|\b[A-Z]{4}MD[A-Z0-9]{2,7}\b|$))",
		latin,
	)
	if not bank_match:
		return None, code
	raw_name = bank_match[1].strip(" ,;:")
	has_quotes = bool(re.search(r"['‘’“”\"]", raw_name))
	name = re.sub(r"\s*['‘’“”\"—|]+\s*", " ", raw_name).strip(" ,;:'")
	name = re.sub(r"\bS\s*\.\s*A\s*\.?$", "SA", name, flags=re.I)
	if has_quotes:
		name_without_suffix = re.sub(r"\s+SA\.?$", "", name).strip()
		name = f'BC "{name_without_suffix}" SA' if name_without_suffix else f"BC {name}"
	else:
		name = f"BC {name}"
	if not re.search(r"\bSA\.?$", name) and re.search(
		r"O\.?F\.?\s*/\s*NR\.?\s*TVA.*?\bSA\b", latin[bank_match.end() :], re.I
	):
		name += " SA"
	return name or None, code


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
	for source_line in text.splitlines():
		line = source_line.replace("|", " ")
		uom = re.search(r"\b(buc|serv)\.?\b", line, re.I)
		if not uom:
			continue
		name = re.sub(r"^[|\s._=+-]+", "", line[: uom.start()]).strip(" |._=+-")
		if len(name) < 3 or "unitate" in name.lower():
			continue
		tail = line[uom.end() :]
		amount = r"(\d{1,3}(?:[ ,]\d{3})+[.,:\-]\d{2}|\d{1,7}[.,:\-]\d{2})"
		match = re.search(
			rf"\b(\d+(?:[.,]\d+)?)\b\s+{amount}\s+{amount}\s+(0|5|8|20)(?:[.,:\-]00)?\s+{amount}\s+{amount}",
			tail,
		)
		if match:
			qty = decimal(_number(match[1]))
			rate, net, vat_rate, vat, gross = map(decimal, (_number(value) for value in match.groups()[1:]))
		else:
			# A narrow quantity cell can disappear in a phone photo. Recover it only from exact row arithmetic.
			match = re.search(
				rf"{amount}\s+{amount}\s+(0|5|8|20)(?:[.,:\-]00)?\s+{amount}\s+{amount}", tail
			)
			if match:
				rate, net, vat_rate, vat, gross = map(decimal, (_number(value) for value in match.groups()))
				qty = net / rate if rate else decimal("0")
			else:
				values = re.findall(
					rf"(?<!\d)(?:{amount[1:-1]}|0|5|8|20)(?!\d)", tail
				)
				candidate = next(
					(
						(index, tuple(decimal(_number(value)) for value in values[index : index + 5]))
						for index in range(len(values) - 4)
						if decimal(_number(values[index + 2])) in (decimal("0"), decimal("5"), decimal("8"), decimal("20"))
					),
					None,
				)
				if not candidate:
					continue
				index, (rate, net, vat_rate, vat, gross) = candidate
				qty = decimal(_number(values[index - 1])) if index and decimal(_number(values[index - 1])) * rate == net else net / rate if rate else decimal("0")
		if vat_rate not in (decimal("0"), decimal("5"), decimal("8"), decimal("20")):
			continue
		if abs(money(qty * rate) - money(net)) > decimal("0.01") or money(net * vat_rate / 100) != money(vat):
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


def _compact_items(text: str) -> list[dict]:
	"""Recover amounts whose decimal separator merged into a table rule (for example, 480000)."""
	items = []
	for source_line in text.splitlines():
		line = source_line.replace("|", " ")
		uom = re.search(r"\b(buc|serv)\.?\b", line, re.I)
		if not uom:
			continue
		name = re.sub(r"^[|\s._=+-]+", "", line[: uom.start()]).strip(" |._=+-")
		tokens = re.findall(r"(?<!\d)\d{1,9}(?!\d)|(?<!\w)[oO](?!\w)", line[uom.end() :])
		if len(name) < 3 or len(tokens) < 6:
			continue
		qty = decimal(tokens[0])
		values = []
		for token in tokens[1:6]:
			token = "0" if token.lower() == "o" else token
			values.append(decimal(token[:-2] + "." + token[-2:]) if len(token) >= 3 else decimal(token))
		rate, net, vat_rate, vat, gross = values
		if vat_rate not in (decimal("0"), decimal("5"), decimal("8"), decimal("20")):
			continue
		if abs(money(qty * rate) - money(net)) > decimal("0.01") or money(net * vat_rate / 100) != money(vat):
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


def _is_total_row(name: str) -> bool:
	return "total" in name.lower()


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
	threshold = cv2.adaptiveThreshold(
		gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15
	)
	horizontal = cv2.morphologyEx(
		threshold, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(250, width // 10), 1))
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
	if len(x_lines) < 10:
		return [], None
	x_lines = x_lines[:10]
	# Removing the rules before recognizing the complete table preserves digits that touch a cell border.
	grid_mask = cv2.dilate(
		cv2.bitwise_or(horizontal, vertical), cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
	)
	without_grid = cv2.inpaint(gray, grid_mask, 3, cv2.INPAINT_TELEA)
	previous_rules = [y for y in y_values if y < y_lines[0] - 90]
	table_top = max(previous_rules, default=y_lines[0])
	table = without_grid[max(0, table_top - 5) : min(height, y_lines[-1] + 5), left : x_lines[-1]]
	table = cv2.resize(table, None, fx=2, fy=2)
	import pytesseract

	table_text = pytesseract.image_to_string(
		table, lang="ron+eng", config="--psm 4 -c preserve_interword_spaces=1"
	)
	whole_rows = _items(table_text)
	whole_totals = _totals(table_text)
	if whole_rows and whole_totals:
		return whole_rows, whole_totals[-1]
	items = []
	totals = None
	for top, bottom in pairwise(y_lines):
		name = _read_cell(gray, top, bottom, x_lines[0], x_lines[1]).strip(" |._-")
		if _is_total_row(name):
			net = _amount_cell(_read_cell(gray, top, bottom, x_lines[4], x_lines[5], numbers=True))
			vat = _amount_cell(_read_cell(gray, top, bottom, x_lines[6], x_lines[7], numbers=True))
			gross = _amount_cell(_read_cell(gray, top, bottom, x_lines[7], x_lines[8], numbers=True))
			if net is not None and vat is not None and gross is not None:
				totals = tuple(map(money, (net, vat, gross)))
			continue
		if len(name) < 3:
			continue
		row_text = _read_cell(gray, top, bottom, x_lines[0], x_lines[9])
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


def _barcode_identity(gray):
	try:
		import zxingcpp
	except ImportError:
		return None
	for barcode in zxingcpp.read_barcodes(gray):
		match = re.fullmatch(r"(A{2}[A-Z])(\d{7})", barcode.text.upper().replace(" ", ""))
		if match:
			return match
	return None


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


def _totals_reconcile(calculated: tuple, detected: tuple) -> bool:
	return (
		detected[1] == calculated[1]
		and detected[2] == calculated[2]
		and money(calculated[0] + calculated[1]) == calculated[2]
	)


def _vat_rates():
	return {decimal("0"), decimal("5"), decimal("8"), decimal("20")}


def _is_retail(text: str) -> bool:
	header = text.upper().translate(_CYRILLIC_LATIN)
	if re.search(r"1[.:\s]+FUR(?:N|M)IZOR", header) and re.search(r"12[.,]?\s*TOTAL", header):
		return False
	return bool(
		re.search(r"BON\s*FISCAL", header)
		or re.search(r"COD\s*ARTICOL", header)
		or "METRO CASH" in header
		or (re.search(r"MOD\s*AMB", header) and re.search(r"VAL\.?\s*TOT", header))
	)


def _near(text: str, token: str, before=280, after=120) -> str:
	match = re.search(re.escape(token), text)
	if not match:
		return ""
	return text[max(0, match.start() - before) : match.end() + after]


def _all_ibans(text: str) -> list[str]:
	found = []
	latin = text.upper().translate(_CYRILLIC_LATIN)
	for match in re.finditer(r"\bMD\s*\d{2}[A-Z0-9\s\\/]{16,28}", latin):
		value = re.sub(r"[^A-Z0-9]", "", match[0])[:24]
		if len(value) >= 24 and value not in found:
			found.append(value)
	return found


def _labeled_amount(text: str, label: str):
	found = None
	for pattern in (rf"(?:{label})\s*[:.]?\s*{_AMOUNT}", rf"{_AMOUNT}\s*(?:{label})"):
		for match in re.finditer(pattern, text, re.I):
			found = money(decimal(_number(match[1])))
	return found


def _retail_footer(text: str) -> str:
	lines = [line for line in text.splitlines() if line.strip()]
	start = next(
		(
			index
			for index, line in enumerate(lines)
			if re.search(r"total\s+cant[iıl1]?tate|total\s+greutate|total\s+pagina", line, re.I)
		),
		None,
	)
	if start is not None:
		return "\n".join(lines[start:])
	tail = []
	for line in reversed(lines):
		if re.search(r"\b\d{13}\b", line) and tail:
			break
		tail.append(line)
		if len(tail) >= 15:
			break
	return "\n".join(reversed(tail))


def _retail_qty_total(text: str):
	match = re.search(r"total\s+cant[iıl1]?tate\s*[:.]?\s*(\d+(?:[.,]\d+)?)", text, re.I)
	return decimal(_number(match[1])) if match else None


def _retail_net_label():
	return r"val\.?\s*tot(?:al(?:a|ă)?)?\.?\s*fara\s*tva|valoare\s+total[aă]?\s+fara\s+tva|tot(?:al)?\.?\s*fara\s*tva"


def _retail_money_totals(*texts: str) -> list[tuple]:
	results = []
	for text in texts:
		footer = _retail_footer(text)
		net = _labeled_amount(footer, _retail_net_label())
		vat = _labeled_amount(footer, r"val(?:oare)?\.?\s+tva(?!\s*%)")
		gross = _labeled_amount(
			footer, r"val\.?\s*tot(?:al(?:a|ă)?)?\.?\s*(?:incl\.?|cu)\s*tva|total\s+de\s+plata"
		)
		if net is not None and vat is not None and gross is not None:
			results.append((net, vat, gross))
		elif net is not None and vat is not None:
			results.append((net, vat, money(net + vat)))
		elif net is not None:
			vat = money(net * 20 / 100)
			results.append((net, vat, money(net + vat)))
	return results


def _retail_token_values(text: str) -> list:
	values = []
	for match in _RETAIL_TOKEN.finditer(text):
		if re.match(r"[A-Za-zăâîșț]", text[match.end() : match.end() + 1] or " "):
			continue
		values.append(decimal(_number(match[1])))
	return values


def _retail_from_gross(values: list):
	values = [value for value in values]
	while values and values[-1] == 0:
		values.pop()
	rates = _vat_rates()
	if len(values) < 3:
		return None
	gross = values[-1]
	vat_rate = next((value for value in reversed(values[:-1]) if value in rates), None)
	if vat_rate is None or gross <= 0:
		return None
	net = money(gross * 100 / (100 + vat_rate))
	vat = money(gross - net)
	if vat_rate and money(net * vat_rate / 100) != vat:
		return None
	qty_candidates = [
		value
		for value in values[:-1]
		if value > 0 and value == value.to_integral_value() and value not in rates and value < gross
	]
	for qty in reversed(qty_candidates):
		rate = money(net / qty)
		if rate > 0 and abs(money(qty * rate) - net) <= decimal("0.01"):
			return qty, rate, net, vat_rate, vat, gross
	return None


def _retail_amounts(values: list):
	rates = _vat_rates()
	for index, vat_rate in enumerate(values):
		if vat_rate not in rates:
			continue
		before, after = values[:index], values[index + 1 :]
		if not before or not after:
			continue
		pairs = [(after[0], after[-1])]
		if len(after) >= 3:
			pairs.append((after[0], after[2]))
		if len(after) == 1:
			net = next((value for value in reversed(before) if value > 0), None)
			if net is not None:
				pairs.append((money(net * vat_rate / 100), after[0]))
		seen = set()
		for vat, gross in pairs:
			key = (vat, gross)
			if key in seen or gross <= 0:
				continue
			seen.add(key)
			for net in reversed(before):
				if money(net * vat_rate / 100) != money(vat) or money(net + vat) != money(gross):
					continue
				for qty in reversed(before):
					if (
						qty > 0
						and qty == qty.to_integral_value()
						and qty not in rates
						and qty <= decimal("1000")
					):
						rate = money(net / qty)
						if rate > 0 and abs(money(qty * rate) - net) <= decimal("0.01"):
							return qty, rate, net, vat_rate, vat, gross
				for qty_index in range(len(before) - 1, -1, -1):
					qty = before[qty_index]
					if qty <= 0:
						continue
					for rate in before[qty_index + 1 :]:
						if rate > 0 and abs(money(qty * rate) - money(net)) <= decimal("0.01"):
							return qty, rate, net, vat_rate, vat, gross
				if net > 0:
					for rate in before:
						if rate > 0:
							qty = net / rate
							if decimal("0.001") <= qty <= decimal("9999") and abs(
								money(qty * rate) - money(net)
							) <= decimal("0.01"):
								return qty, rate, net, vat_rate, vat, gross
	return _retail_from_gross(values)


def _retail_row(line: str) -> dict | None:
	if re.search(r"PL\s*/\s*PA|total\s+cant|val\.?\s*tot|cod\s*articol", line, re.I):
		return None
	ean = re.search(r"\b(\d{13}|\d{8})\b", line)
	if (
		not ean
		or (len(ean[1]) == 13 and ean[1].startswith("1"))
		or (len(ean[1]) == 8 and ean[1].startswith("20"))
		or re.search(r"\bIBAN\b", line, re.I)
	):
		return None
	rest = line[ean.end() :]
	pack = re.search(r"\b(IM|ST|BU|TP|CS|PK|CT|KG|SET|UN)\b", rest, re.I)
	if pack:
		name = rest[: pack.start()]
	else:
		name = re.split(r"\s+\d+(?:[.,]\d+)?(?:\s+\d+(?:[.,]\d+)?){2,}\s*$", rest)[0]
	name = " ".join(name.split()).strip(" |._=+-")
	name = re.sub(r"\s+\d+$", "", name)
	if len(name) < 3:
		return None
	values = _retail_token_values(rest[pack.end() :] if pack else rest)
	parsed = _retail_amounts(values)
	if not parsed:
		return None
	qty, rate, net, vat_rate, vat, gross = parsed
	return {
		"description": name,
		"supplier_item_code": ean[1],
		"source_uom": pack[1].upper() if pack else "",
		"source_qty": str(qty),
		"source_rate": str(rate),
		"net_amount": str(net),
		"vat_rate": str(vat_rate),
		"vat_amount": str(vat),
		"amount": str(gross),
		"qty": str(qty),
	}


def _retail_joined_lines(text: str) -> list[str]:
	lines = text.splitlines()
	joined = []
	index = 0
	while index < len(lines):
		line = lines[index]
		nxt = lines[index + 1] if index + 1 < len(lines) else ""
		if (
			re.search(r"\b(\d{13}|\d{8})\b", line)
			and nxt
			and not re.search(r"\b(\d{13}|\d{8})\b", nxt)
			and not re.search(r"[A-Za-zăâîșț]{3,}", nxt)
			and not re.search(r"PL\s*/\s*PA|total\s+cant", nxt, re.I)
			and _retail_row(line) is None
			and _retail_row(line + " " + nxt)
		):
			joined.append(line + " " + nxt)
			index += 2
			continue
		joined.append(line)
		index += 1
	return joined


def _prefer_retail_row(current: dict, candidate: dict) -> dict:
	if candidate.get("source_uom") and not current.get("source_uom"):
		return candidate
	return current


def _retail_items(text: str) -> list[dict]:
	ordered: list[dict] = []
	index_by_ean: dict[str, int] = {}
	for line in _retail_joined_lines(text):
		row = _retail_row(line)
		if not row:
			continue
		code = row["supplier_item_code"]
		if code in index_by_ean:
			idx = index_by_ean[code]
			ordered[idx] = _prefer_retail_row(ordered[idx], row)
			continue
		index_by_ean[code] = len(ordered)
		ordered.append(row)
	return ordered


def _merge_retail_items(*item_sets: list[dict]) -> list[dict]:
	ordered: list[dict] = []
	index_by_ean: dict[str, int] = {}
	for rows in item_sets:
		for row in rows:
			code = row["supplier_item_code"]
			if code in index_by_ean:
				idx = index_by_ean[code]
				ordered[idx] = _prefer_retail_row(ordered[idx], row)
				continue
			index_by_ean[code] = len(ordered)
			ordered.append(row)
	return ordered


def _name_before_idno(text: str, idno: str) -> str:
	pos = text.find(idno)
	chunk = text[max(0, pos - 220) : pos] if pos >= 0 else text
	matches = list(re.finditer(r"[A-ZĂÂÎȘȚI.C][^,\n]{2,80}?\b(?:S\.?R\.?L\.?|S\.?A\.?)", chunk, re.I))
	if matches:
		return _party_name(matches[-1][0]) or matches[-1][0].strip()
	return _party_name(chunk)


def _address_near_idno(text: str, name: str, idno: str) -> str:
	pos = text.find(idno)
	if pos < 0:
		return ""
	after = text[pos + len(idno) : pos + len(idno) + 200]
	after = re.split(
		r"\s*/\s*\d{7}\b|\bIBAN\b|\bSERIA\b|\bBON\s*FISCAL\b|\b1\d{12}\b",
		after,
		maxsplit=1,
		flags=re.I,
	)[0]
	before = ""
	if name:
		idx = text.rfind(name, 0, pos)
		if idx >= 0:
			before = text[idx + len(name) : pos]
	return re.sub(r"\bIDNO\b[:\s]*", "", _party_address(f"{name} {before} {after}"), flags=re.I).strip(" ,;:")


def _retail_parties(text: str):
	pairs = re.findall(r"\b(1\d{12})\s*/\s*(\d{7})\b", text)
	idnos = list(dict.fromkeys(re.findall(r"\b(1\d{12})\b", text)))
	supplier_vat = customer_vat = ""
	if len(pairs) >= 2:
		supplier_idno, supplier_vat = pairs[0]
		customer_idno, customer_vat = pairs[1]
	elif len(pairs) == 1 and len(idnos) >= 2:
		paired_id, paired_vat = pairs[0]
		other = next((value for value in idnos if value != paired_id), None)
		if not other:
			return None
		if idnos[0] == paired_id:
			supplier_idno, supplier_vat = paired_id, paired_vat
			customer_idno = other
		else:
			supplier_idno = other
			customer_idno, customer_vat = paired_id, paired_vat
	elif len(idnos) >= 2:
		supplier_idno, customer_idno = idnos[0], idnos[1]
	else:
		return None
	supplier_block = _near(text, supplier_idno)
	customer_block = _near(text, customer_idno)
	supplier_name = _name_before_idno(text, supplier_idno)
	if not supplier_name:
		match = re.search(r"(I\.C\.S\.?\s+METRO CASH[^\n]{0,80}S\.?R\.?L\.?)", text, re.I)
		supplier_name = match[1] if match else ""
	customer_name = _name_before_idno(text, customer_idno)
	if not supplier_name or not customer_name:
		return None
	ibans = _all_ibans(text)
	supplier_bank_name, supplier_bank_code = _bank_details(supplier_block)
	customer_bank_name, customer_bank_code = _bank_details(customer_block)
	if ibans:
		if not supplier_bank_code:
			supplier_bank_name, supplier_bank_code = _bank_details(_near(text, ibans[0]))
		if len(ibans) > 1 and not customer_bank_code:
			customer_bank_name, customer_bank_code = _bank_details(_near(text, ibans[1]))
	related = re.search(r"bon\s*fiscal\s*(?:nr\.?)?\s*(\d+)", text, re.I)
	account = re.search(r"(?:nr\.?\s*)?client(?:\s*nr\.?)?\s*[:.]?\s*([0-9 ]+[A-Z]{0,3})", text, re.I)
	return {
		"supplier_name": supplier_name,
		"supplier_idno": supplier_idno,
		"supplier_vat_id": supplier_vat,
		"supplier_address": _address_near_idno(text, supplier_name, supplier_idno),
		"supplier_bank_account": ibans[0] if ibans else _iban(supplier_block),
		"supplier_bank_name": supplier_bank_name,
		"supplier_bank_code": supplier_bank_code,
		"buyer_name": customer_name,
		"buyer_idno": customer_idno,
		"buyer_vat_id": customer_vat,
		"buyer_address": _address_near_idno(text, customer_name, customer_idno),
		"buyer_bank_account": ibans[1] if len(ibans) > 1 else _iban(customer_block),
		"buyer_bank_name": customer_bank_name,
		"buyer_bank_code": customer_bank_code,
		"related_document_type": "Bon fiscal" if related else None,
		"related_document_number": related[1] if related else None,
		"provider_account": " ".join(account[1].split()) if account else None,
	}


def _retail_qty_sum(items: list[dict]):
	return sum((decimal(row["source_qty"]) for row in items), decimal("0"))


def _retail_calculated(items: list[dict]) -> tuple:
	return tuple(
		money(sum(decimal(row[key]) for row in items)) for key in ("net_amount", "vat_amount", "amount")
	)


def _retail_totals_match(items: list[dict], *texts: str):
	calculated = _retail_calculated(items)
	if any(_totals_reconcile(calculated, candidate) for candidate in _retail_money_totals(*texts)):
		return calculated
	qty_printed = next(
		(value for text in texts if (value := _retail_qty_total(_retail_footer(text))) is not None),
		None,
	)
	net_printed = next(
		(
			value
			for text in texts
			if (value := _labeled_amount(_retail_footer(text), _retail_net_label())) is not None
		),
		None,
	)
	if qty_printed is not None and qty_printed == _retail_qty_sum(items) and (
		net_printed is None or net_printed == calculated[0]
	):
		return calculated
	return None


def _retail_matched_totals(items: list[dict], *texts: str):
	matched = _retail_totals_match(items, *texts)
	if matched:
		return matched
	calculated = _retail_calculated(items)
	candidates = _retail_money_totals(*texts)
	detected = ", ".join("/".join(map(str, candidate)) for candidate in candidates) or "none"
	qty_printed = next(
		(value for text in texts if (value := _retail_qty_total(_retail_footer(text))) is not None),
		None,
	)
	net_printed = next(
		(
			value
			for text in texts
			if (value := _labeled_amount(_retail_footer(text), _retail_net_label())) is not None
		),
		None,
	)
	if net_printed is not None:
		detected = f"{detected}; printed net {net_printed}"
	if qty_printed is not None:
		detected = f"{detected}; printed qty {qty_printed} vs items {_retail_qty_sum(items)}"
	_fail(
		"item totals do not match the factura totals "
		f"(calculated net/VAT/total: {'/'.join(map(str, calculated))}; detected: {detected})"
	)


def parse_retail_text(text: str, *more: str) -> dict:
	combined = "\n".join((text, *more))
	header = combined.upper().translate(_CYRILLIC_LATIN)
	identity = _IDENTITY.search(header)
	issue_date = _date(combined)
	parties = _retail_parties(combined)
	sources = [text, *more]
	item_sets = [rows for source in sources if (rows := _retail_items(source))]
	if len(item_sets) > 1:
		item_sets.append(_merge_retail_items(*item_sets))
	items = next(
		(rows for rows in item_sets if _retail_totals_match(rows, combined)),
		item_sets[0] if item_sets else [],
	)
	missing = []
	if not identity:
		missing.append("invoice series and number")
	if not issue_date:
		missing.append("issue date")
	if not parties:
		missing.append("supplier and customer IDNO/VAT numbers")
	if not items:
		missing.append("complete item rows")
	if missing:
		_fail("could not extract " + ", ".join(missing))
	matched_totals = _retail_matched_totals(items, combined)
	result = {
		"provider": "OCR",
		"series": identity[1],
		"number": identity[2],
		"issue_date": issue_date,
		"delivery_date": issue_date,
		"currency": "MDL",
		"net_total": str(matched_totals[0]),
		"vat_total": str(matched_totals[1]),
		"total": str(matched_totals[2]),
		"items": items,
		"original_format": "Paper",
		"signature_status": "Not Applicable",
		"signature_details": [],
	}
	result.update({key: value for key, value in parties.items() if value is not None})
	return result


def parse_image(content: bytes) -> dict:
	from erpnext_moldova_efactura.utils.factura_ai import parse_image as parse_ai

	return parse_ai(content)
