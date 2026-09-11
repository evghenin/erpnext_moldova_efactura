"""Gemini vision import for photographed Moldovan fiscal invoices.

The original file stays in ERPNext. A copy of the image is sent to Google Gemini
for structured extraction. A draft is created only when identity, parties, every
item and totals reconcile locally.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money

MAX_IMAGE_BYTES = 15 * 1024 * 1024
OCR_ERROR = "Cannot read factura"
OCR_GUIDANCE = "Please provide a clearer, correctly oriented photo or scan of the complete document."
DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE_HTTP = {429, 503}
RETRY_WAIT_SECONDS = (2, 5, 10)
GEMINI_TIMEOUT = 180
MAX_GEMINI_EDGE = 2200
RETIRED_MODELS = {
	"gemini-2.0-flash": DEFAULT_MODEL,
	"gemini-2.5-flash": DEFAULT_MODEL,
	"gemini-2.5-flash-lite": DEFAULT_MODEL,
}
PROMPT = """Extract this Moldovan fiscal invoice (factură fiscală) as JSON.

Rules:
- Read every charged item row once. Do not omit rows, invent rows, duplicate a numbered line, or guess missing numbers.
- A charged row starts with Cod articol / EAN (digits, sometimes prefixed with M). Continuation notes such as PL/PA, discounts of 0, column headers, 12. TOTAL, Total pagina, Total cantitate, Total greutate, weight and cashier/till labels are not items.
- The Reducere column is not a separate item. Footer REDUCERE CANTITATIVA / minus-amount codes (for example 250075348) only repeat that column; omit them.
- On a charged row, amount is Valoare incl. TVA after Reducere. Reduce net_amount and vat_amount by the same Reducere (incl. VAT) at the row VAT rate. source_qty and source_rate stay Cant. and Pret unitar. Val. tot. fara TVA is already after Reducere.
- Total cantitate is the sum of source_qty, not the number of rows. After extraction, the sum of item net_amount must equal Val. tot. fara TVA.
- net_total is Val. tot. fara TVA / Valoarea fara TVA for the whole factura, never Total pagina.
- vat_total and total are the document VAT and payable gross. Do not use Total pagina as net_total or as the payable total.
- Fiscal identity is SERIA + NR (for example AAQ and 1838180). Bon fiscal / act / account stay related_document, not the factura number.
- source_qty is Cantitate / Vanz., which may be greater than 1. source_rate is Pret unitar, not Pret colet.
- source_uom is the printed packing/UOM (Mod amb., buc, serv). supplier_item_code is Cod articol/EAN when printed.
- Amounts use a dot decimal separator. VAT rates are 0, 5, 8 or 20.
- IDNO is 13 digits starting with 1. VAT ID is 7 digits when printed; leave blank if absent. Do not invent VAT IDs.
- METRO / till layouts: supplier is the left header (METRO IDNO 1004601002738). Buyer is the right Cumpărător / Nr. Client block. Never copy the supplier IDNO onto the buyer.
- COD FISCAL/NR. TVA is IDNO, a slash, then VAT. Split them. Right-side buyer example: 1024600026571/0211775 → buyer_idno 1024600026571, buyer_vat_id 0211775.
- Dates as YYYY-MM-DD. Preserve printed names, addresses and IBANs. Currency is MDL unless another ISO code is printed.
- net_total, vat_total and total must be the document totals printed on the factura, not the sum of a duplicated line."""

RESPONSE_SCHEMA = {
	"type": "OBJECT",
	"properties": {
		"series": {"type": "STRING"},
		"number": {"type": "STRING"},
		"issue_date": {"type": "STRING"},
		"delivery_date": {"type": "STRING"},
		"currency": {"type": "STRING"},
		"supplier_name": {"type": "STRING"},
		"supplier_idno": {"type": "STRING"},
		"supplier_vat_id": {"type": "STRING"},
		"supplier_address": {"type": "STRING"},
		"supplier_bank_account": {"type": "STRING"},
		"supplier_bank_name": {"type": "STRING"},
		"supplier_bank_code": {"type": "STRING"},
		"buyer_name": {"type": "STRING"},
		"buyer_idno": {"type": "STRING"},
		"buyer_vat_id": {"type": "STRING"},
		"buyer_address": {"type": "STRING"},
		"buyer_bank_account": {"type": "STRING"},
		"buyer_bank_name": {"type": "STRING"},
		"buyer_bank_code": {"type": "STRING"},
		"related_document_type": {"type": "STRING"},
		"related_document_number": {"type": "STRING"},
		"related_document_date": {"type": "STRING"},
		"net_total": {"type": "STRING"},
		"vat_total": {"type": "STRING"},
		"total": {"type": "STRING"},
		"items": {
			"type": "ARRAY",
			"items": {
				"type": "OBJECT",
				"properties": {
					"description": {"type": "STRING"},
					"supplier_item_code": {"type": "STRING"},
					"source_uom": {"type": "STRING"},
					"source_qty": {"type": "STRING"},
					"source_rate": {"type": "STRING"},
					"net_amount": {"type": "STRING"},
					"vat_rate": {"type": "STRING"},
					"vat_amount": {"type": "STRING"},
					"amount": {"type": "STRING"},
				},
				"required": [
					"description",
					"source_qty",
					"source_rate",
					"net_amount",
					"vat_rate",
					"vat_amount",
					"amount",
				],
			},
		},
	},
	"required": [
		"series",
		"number",
		"issue_date",
		"supplier_name",
		"supplier_idno",
		"buyer_name",
		"buyer_idno",
		"net_total",
		"vat_total",
		"total",
		"items",
	],
}


def _fail(reason: str):
	raise FacturaImportError(f"{OCR_ERROR}: {reason}. {OCR_GUIDANCE}")


def _service_fail(reason: str):
	raise FacturaImportError(f"{OCR_ERROR}: {reason}")


def _blank(value) -> str:
	return " ".join(str(value or "").split())


def _digits(value: str) -> str:
	return re.sub(r"\D", "", value or "")


def _idno(value: str) -> str:
	"""Accept a 13-digit IDNO, or COD FISCAL/NR.TVA glued as 13+7 digits."""
	digits = _digits(value)
	if len(digits) == 20 and digits.startswith("1"):
		return digits[:13]
	return digits


def _vat_id(value: str, idno_raw: str = "") -> str:
	digits = _digits(value)
	if len(digits) == 7:
		return digits
	glued = _digits(idno_raw)
	if len(glued) == 20 and glued.startswith("1"):
		return glued[13:]
	return digits


def _date(value: str) -> str | None:
	value = _blank(value)
	if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
		try:
			datetime.strptime(value, "%Y-%m-%d")
			return value
		except ValueError:
			return None
	match = re.search(r"\b(\d{2})[.\-/](\d{2})[.\-/](20\d{2})\b", value)
	if match:
		return f"{match[3]}-{match[2]}-{match[1]}"
	return None


def _iban(value: str) -> str:
	value = re.sub(r"[^A-Z0-9]", "", _blank(value).upper())
	return value[:24] if value.startswith("MD") and len(value) >= 24 else ""


def _qty_from_net(qty, rate, net):
	"""METRO Unit/Vanz. mix-ups send qty=1; recover Van. from printed net / Pret unitar."""
	if abs(money(qty * rate) - net) <= decimal("0.05"):
		return qty
	if rate <= 0:
		return None
	whole = (net / rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	if whole >= 1 and abs(money(whole * rate) - net) <= decimal("0.05"):
		return whole
	return None


def _vat_rate_for(net, vat):
	for rate in (decimal("20"), decimal("8"), decimal("5"), decimal("0")):
		if abs(money(net * rate / 100) - vat) <= decimal("0.05"):
			return rate
	return None


def _optional_decimal(value, *, percent=False):
	text = _blank(value).replace("−", "-").replace(",", ".")
	if percent:
		text = text.replace("%", "")
	text = re.sub(r"\s+", "", text)
	if text.endswith("-") and not text.startswith("-"):
		text = "-" + text[:-1]
	if not text or text in {".", "-"}:
		return None
	match = re.search(r"-?\d+(?:\.\d+)?", text)
	if not match:
		return None
	return decimal(match.group(0))


def _item(raw: dict) -> dict | None:
	try:
		qty = _optional_decimal(raw.get("source_qty"))
		rate = _optional_decimal(raw.get("source_rate"))
		net_raw = _optional_decimal(raw.get("net_amount"))
		vat_rate = _optional_decimal(raw.get("vat_rate"), percent=True)
		vat = _optional_decimal(raw.get("vat_amount"))
		gross_raw = _optional_decimal(raw.get("amount"))
		if net_raw is None or gross_raw is None:
			return None
		net = money(net_raw)
		gross = money(gross_raw)
	except FacturaImportError:
		return None
	if vat is not None:
		vat = money(vat)
	name = _blank(raw.get("description"))
	if len(name) < 3:
		return None
	if _is_reducere_row(name, net, gross):
		return None
	if qty is None or qty <= 0:
		return None
	if rate is None or rate < 0 or gross <= 0 or net < 0:
		return None
	if vat is None:
		return None
	if vat_rate not in (decimal("0"), decimal("5"), decimal("8"), decimal("20")):
		vat_rate = _vat_rate_for(net, vat)
	if vat_rate is None:
		return None
	qty = _qty_from_net(qty, rate, net)
	if qty is None:
		return None
	applied = _apply_row_reducere(net, vat, gross, vat_rate)
	if applied is None:
		return None
	net, vat, gross = applied
	if abs(money(net * vat_rate / 100) - vat) > decimal("0.05"):
		return None
	item = {
		"description": name,
		"source_uom": _blank(raw.get("source_uom")),
		"source_qty": str(qty),
		"source_rate": str(rate),
		"net_amount": str(net),
		"vat_rate": str(vat_rate),
		"vat_amount": str(vat),
		"amount": str(gross),
		"qty": str(qty),
	}
	code = _digits(_blank(raw.get("supplier_item_code")))
	if code:
		item["supplier_item_code"] = code
	return item


FOOTER_TOLERANCE = decimal("0.20")
_REDUCERE_NAME = re.compile(r"reducere\s+cantitativa|quantity\s+discount", re.I)


def _is_reducere_row(name: str, net, gross) -> bool:
	return bool(_REDUCERE_NAME.search(name)) or net < 0 or gross < 0


def _apply_row_reducere(net, vat, gross, vat_rate):
	"""Fold the Reducere column into the charged row. Keep qty and Pret unitar."""
	pre_gross = money(net + vat)
	if pre_gross == gross:
		return net, vat, gross
	if net <= 0 or pre_gross < gross:
		return None
	discount_gross = money(pre_gross - gross)
	if discount_gross <= decimal("0.05"):
		return net, vat, pre_gross
	discount_net = money(discount_gross * 100 / (100 + vat_rate))
	discount_vat = money(discount_gross - discount_net)
	net = money(net - discount_net)
	vat = money(vat - discount_vat)
	if net <= 0 or vat < 0 or money(net + vat) != gross:
		return None
	return net, vat, gross


def _item_sums(items: list[dict]) -> tuple:
	return tuple(money(sum(decimal(row[key]) for row in items)) for key in ("net_amount", "vat_amount", "amount"))


def _ai_totals_reconcile(calculated: tuple, detected: tuple) -> bool:
	if money(calculated[0] + calculated[1]) != calculated[2]:
		return False
	if money(detected[0] + detected[1]) != detected[2]:
		return False
	return all(abs(calculated[i] - detected[i]) <= FOOTER_TOLERANCE for i in range(3))


def _document_totals(calculated: tuple, detected: tuple) -> tuple:
	"""Prefer printed Val. tot. fara TVA; keep payable gross from charged rows."""
	net = detected[0] if abs(calculated[0] - detected[0]) <= FOOTER_TOLERANCE else calculated[0]
	total = calculated[2]
	return money(net), money(total - net), money(total)


def _row_key(row: dict) -> tuple:
	return (
		row["description"].casefold(),
		row["source_qty"],
		row["source_rate"],
		row["net_amount"],
		row["amount"],
	)


def _row_amounts(row: dict) -> tuple:
	return tuple(decimal(row[key]) for key in ("net_amount", "vat_amount", "amount"))


def _drop_duplicate_items(items: list[dict], detected: tuple) -> list[dict] | None:
	"""Drop extra copies of the same charged line when the printed totals match the remainder."""
	if _ai_totals_reconcile(_item_sums(items), detected):
		return items
	seen: dict[tuple, int] = {}
	for index, row in enumerate(items):
		key = _row_key(row)
		if key in seen:
			remaining = items[:index] + items[index + 1 :]
			matched = _drop_duplicate_items(remaining, detected)
			if matched is not None:
				return matched
		else:
			seen[key] = index
	matched = _drop_overage_row(items, detected)
	if matched is not None:
		return matched
	matched = _drop_overage_rows(items, detected)
	if matched is not None:
		return matched
	matched = _apply_surplus_reducere(items, detected)
	if matched is not None:
		return matched
	for index in range(len(items)):
		remaining = items[:index] + items[index + 1 :]
		if not remaining:
			continue
		repaired = _apply_surplus_reducere(remaining, detected)
		if repaired is not None:
			return repaired
	return None


def _apply_surplus_reducere(items: list[dict], detected: tuple) -> list[dict] | None:
	"""When Reducere was omitted from every amount, fold a same-rate surplus into a charged row."""
	calculated = _item_sums(items)
	if _ai_totals_reconcile(calculated, detected):
		return items
	surplus = tuple(calculated[i] - detected[i] for i in range(3))
	if surplus[2] <= FOOTER_TOLERANCE or money(surplus[0] + surplus[1]) != surplus[2]:
		return None
	vat_rate = _vat_rate_for(surplus[0], surplus[1])
	if vat_rate is None:
		return None
	for index in range(len(items) - 1, -1, -1):
		row = items[index]
		if decimal(row["vat_rate"]) != vat_rate:
			continue
		net = money(decimal(row["net_amount"]) - surplus[0])
		vat = money(decimal(row["vat_amount"]) - surplus[1])
		gross = money(decimal(row["amount"]) - surplus[2])
		if net <= 0 or vat < 0 or gross <= 0:
			continue
		if money(net + vat) != gross:
			continue
		if abs(money(net * vat_rate / 100) - vat) > decimal("0.05"):
			continue
		updated = dict(row)
		updated["net_amount"] = str(net)
		updated["vat_amount"] = str(vat)
		updated["amount"] = str(gross)
		combined = items[:index] + [updated] + items[index + 1 :]
		if _ai_totals_reconcile(_item_sums(combined), detected):
			return combined
	return None


def _drop_overage_row(items: list[dict], detected: tuple) -> list[dict] | None:
	"""Drop one extra line whose amounts equal the surplus over printed totals."""
	calculated = _item_sums(items)
	surplus = tuple(calculated[i] - detected[i] for i in range(3))
	if surplus[2] <= FOOTER_TOLERANCE:
		return None
	names = [row["description"].casefold() for row in items]
	ranked = []
	for index, row in enumerate(items):
		amounts = _row_amounts(row)
		if any(abs(amounts[i] - surplus[i]) > FOOTER_TOLERANCE for i in range(3)):
			continue
		ranked.append((0 if names.count(names[index]) > 1 else 1, index))
	for _, index in sorted(ranked):
		remaining = items[:index] + items[index + 1 :]
		if remaining and _ai_totals_reconcile(_item_sums(remaining), detected):
			return remaining
	return None


def _drop_overage_rows(items: list[dict], detected: tuple, max_drop: int = 3) -> list[dict] | None:
	"""Drop a small set of extra lines whose amounts sum to the surplus over printed totals."""
	calculated = _item_sums(items)
	surplus = tuple(calculated[i] - detected[i] for i in range(3))
	if surplus[2] <= FOOTER_TOLERANCE:
		return None
	limit = min(max_drop, len(items) - 1)
	for count in range(2, limit + 1):
		for combo in combinations(range(len(items)), count):
			amounts = tuple(sum(_row_amounts(items[index])[i] for index in combo) for i in range(3))
			if any(abs(money(amounts[i]) - surplus[i]) > FOOTER_TOLERANCE for i in range(3)):
				continue
			drop = set(combo)
			remaining = [row for index, row in enumerate(items) if index not in drop]
			if remaining and _ai_totals_reconcile(_item_sums(remaining), detected):
				return remaining
	return None


def document_from_extraction(data: dict, content: bytes) -> dict:
	"""Validate Gemini JSON against factura arithmetic. Used by parse_image and tests."""
	if not isinstance(data, dict):
		_fail("the AI response was not a factura object")
	series = re.sub(r"[^A-Z]", "", _blank(data.get("series")).upper())
	number = _digits(_blank(data.get("number")))
	issue_date = _date(_blank(data.get("issue_date")))
	delivery_date = _date(_blank(data.get("delivery_date"))) or issue_date
	supplier_idno = _idno(_blank(data.get("supplier_idno")))
	buyer_idno = _idno(_blank(data.get("buyer_idno")))
	items = [_item(row) for row in data.get("items") or [] if isinstance(row, dict)]
	items = [row for row in items if row]
	missing = []
	if len(series) < 2 or len(number) < 6:
		missing.append("invoice series and number")
	if not issue_date:
		missing.append("issue date")
	if len(supplier_idno) != 13 or not supplier_idno.startswith("1") or not _blank(data.get("supplier_name")):
		missing.append("supplier identity")
	if len(buyer_idno) != 13 or not buyer_idno.startswith("1") or not _blank(data.get("buyer_name")):
		missing.append("customer identity")
	elif buyer_idno == supplier_idno:
		missing.append("customer identity (buyer IDNO must not copy the supplier)")
	if not items:
		missing.append("complete item rows")
	if missing:
		_fail("could not extract " + ", ".join(missing))
	try:
		detected = tuple(
			money(_blank(data.get(key)).replace(",", ".")) for key in ("net_total", "vat_total", "total")
		)
	except FacturaImportError:
		_fail("could not read document totals")
	extracted = items
	items = _drop_duplicate_items(items, detected)
	if items is None:
		calculated = _item_sums(extracted)
		_fail(
			"item totals do not match the factura totals "
			f"(calculated net/VAT/total: {'/'.join(map(str, calculated))}; "
			f"detected: {'/'.join(map(str, detected))})"
		)
	calculated = _item_sums(items)
	if not _ai_totals_reconcile(calculated, detected):
		_fail(
			"item totals do not match the factura totals "
			f"(calculated net/VAT/total: {'/'.join(map(str, calculated))}; "
			f"detected: {'/'.join(map(str, detected))})"
		)
	net_total, vat_total, total = _document_totals(calculated, detected)
	result = {
		"provider": "Gemini",
		"series": series,
		"number": number,
		"issue_date": issue_date,
		"delivery_date": delivery_date,
		"supplier_name": _blank(data.get("supplier_name")),
		"supplier_idno": supplier_idno,
		"supplier_vat_id": _vat_id(data.get("supplier_vat_id"), data.get("supplier_idno")) or None,
		"supplier_address": _blank(data.get("supplier_address")) or None,
		"supplier_bank_account": _iban(_blank(data.get("supplier_bank_account"))) or None,
		"supplier_bank_name": _blank(data.get("supplier_bank_name")) or None,
		"supplier_bank_code": _blank(data.get("supplier_bank_code")) or None,
		"buyer_name": _blank(data.get("buyer_name")),
		"buyer_idno": buyer_idno,
		"buyer_vat_id": _vat_id(data.get("buyer_vat_id"), data.get("buyer_idno")) or None,
		"buyer_address": _blank(data.get("buyer_address")) or None,
		"buyer_bank_account": _iban(_blank(data.get("buyer_bank_account"))) or None,
		"buyer_bank_name": _blank(data.get("buyer_bank_name")) or None,
		"buyer_bank_code": _blank(data.get("buyer_bank_code")) or None,
		"currency": _blank(data.get("currency")).upper() or "MDL",
		"net_total": str(net_total),
		"vat_total": str(vat_total),
		"total": str(total),
		"items": items,
		"original_format": "Paper",
		"signature_status": "Not Applicable",
		"file_hash": hashlib.sha256(content).hexdigest(),
	}
	related_type = _blank(data.get("related_document_type"))
	related_number = _blank(data.get("related_document_number"))
	if related_type and related_number:
		result["related_document_type"] = related_type
		result["related_document_number"] = related_number
		related_date = _date(_blank(data.get("related_document_date")))
		if related_date:
			result["related_document_date"] = related_date
	return {key: value for key, value in result.items() if value is not None}


def paper_ai_enabled() -> bool:
	if os.environ.get("GEMINI_API_KEY", "").strip():
		return True
	try:
		import frappe

		return bool(frappe.db.get_single_value("eFactura Settings", "gemini_api_key"))
	except Exception:
		return False


def _credentials() -> tuple[str, str]:
	key = os.environ.get("GEMINI_API_KEY", "").strip()
	model = os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL
	try:
		import frappe
		from frappe.utils.password import get_decrypted_password

		stored = get_decrypted_password(
			"eFactura Settings", "eFactura Settings", "gemini_api_key", raise_exception=False
		)
		if stored:
			key = stored.strip()
		stored_model = frappe.db.get_single_value("eFactura Settings", "gemini_model")
		if stored_model:
			model = str(stored_model).strip() or model
	except Exception:
		pass
	model = RETIRED_MODELS.get(model, model)
	if not key:
		_service_fail("set the Gemini API key in eFactura Settings (Purchase → Paper Import)")
	if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
		_service_fail("the Gemini model name is invalid")
	return key, model


def _for_gemini(content: bytes, mime: str) -> tuple[bytes, str]:
	"""Send a smaller JPEG copy. The original file and hash stay unchanged."""
	if mime not in ("image/jpeg", "image/png"):
		return content, mime
	try:
		from PIL import Image

		image = Image.open(io.BytesIO(content))
		if image.mode not in ("RGB", "L"):
			image = image.convert("RGB")
		elif image.mode == "L":
			image = image.convert("RGB")
		width, height = image.size
		edge = max(width, height)
		if edge > MAX_GEMINI_EDGE:
			scale = MAX_GEMINI_EDGE / edge
			size = (max(1, int(width * scale)), max(1, int(height * scale)))
			image = image.resize(size, Image.Resampling.LANCZOS)
		buffer = io.BytesIO()
		image.save(buffer, format="JPEG", quality=82, optimize=True)
		out = buffer.getvalue()
		if out and len(out) < len(content):
			return out, "image/jpeg"
	except Exception:
		pass
	return content, mime


def _correction_text(previous: dict, reason: str) -> str:
	hint = (
		"The previous extraction failed local checks: "
		+ reason
		+ "\nRe-read every charged Cod articol row from the first item to Total cantitate. "
		"Do not add REDUCERE CANTITATIVA as items; apply the Reducere column on those rows. "
		"Keep printed Val. tot. fara TVA / VAT / payable totals. "
	)
	if "customer identity" in reason:
		hint += (
			"Re-read the right Cumpărător COD FISCAL/NR. TVA (13 digits before the slash). "
			"Do not copy the left METRO supplier IDNO onto buyer_idno. "
		)
	return (
		PROMPT
		+ "\n\n"
		+ hint
		+ "Return a complete JSON factura.\nPrevious JSON:\n"
		+ json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
	)


def _generate(content: bytes, mime: str, key: str, model: str, prompt: str | None = None) -> dict:
	body = json.dumps(
		{
			"contents": [
				{
					"parts": [
						{"text": prompt or PROMPT},
						{"inline_data": {"mime_type": mime, "data": base64.b64encode(content).decode()}},
					]
				}
			],
			"generationConfig": {
				"temperature": 0,
				"maxOutputTokens": 65536,
				"responseMimeType": "application/json",
				"responseSchema": RESPONSE_SCHEMA,
			},
		}
	).encode()
	payload = _post_gemini(body, key, model)
	try:
		text = payload["candidates"][0]["content"]["parts"][0]["text"]
		return json.loads(text)
	except (KeyError, IndexError, TypeError, json.JSONDecodeError):
		_service_fail("the AI response was empty or not valid JSON")


def _suggested_model(detail: str) -> str | None:
	for name in re.findall(r"models/(gemini-[A-Za-z0-9._-]+)", detail or ""):
		if name not in RETIRED_MODELS:
			return name
	return None


def _post_gemini(body: bytes, key: str, model: str, *, retried_model: str | None = None) -> dict:
	url = GEMINI_URL.format(model=model) + "?key=" + urllib.parse.quote(key, safe="")
	for attempt, wait in enumerate((0, *RETRY_WAIT_SECONDS)):
		if wait:
			time.sleep(wait)
		request = urllib.request.Request(
			url, data=body, headers={"Content-Type": "application/json"}, method="POST"
		)
		try:
			with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT) as response:
				return json.loads(response.read().decode())
		except urllib.error.HTTPError as exc:
			detail = exc.read().decode(errors="replace")[:400] or str(exc.reason)
			if exc.code == 404:
				suggested = _suggested_model(detail)
				if suggested and suggested != model and retried_model is None:
					return _post_gemini(body, key, suggested, retried_model=suggested)
				_service_fail(
					f"Gemini model {model} is not available. "
					f"Set Gemini Model in eFactura Settings to {DEFAULT_MODEL}"
				)
			if exc.code in RETRYABLE_HTTP and attempt < len(RETRY_WAIT_SECONDS):
				continue
			if exc.code in RETRYABLE_HTTP:
				_service_fail(
					f"Gemini model {model} is busy (HTTP {exc.code}). Try the import again in a minute"
				)
			_service_fail(f"the Gemini API returned HTTP {exc.code}: {detail}")
		except TimeoutError:
			_service_fail("the Gemini request timed out. Try the import again")
		except urllib.error.URLError as exc:
			reason = str(getattr(exc, "reason", exc)).lower()
			if "timed out" in reason or "timeout" in reason:
				_service_fail("the Gemini request timed out. Try the import again")
			if attempt < len(RETRY_WAIT_SECONDS):
				continue
			_service_fail("the Gemini API could not be reached")
		except Exception:
			if attempt < len(RETRY_WAIT_SECONDS):
				continue
			_service_fail("the Gemini API could not be reached")
	_service_fail(f"Gemini model {model} is busy. Try the import again in a minute")


def parse_image(content: bytes) -> dict:
	if len(content) > MAX_IMAGE_BYTES:
		_fail("the source is not a supported JPEG, PNG or PDF file, or it exceeds 15 MB")
	if content.startswith(b"\xff\xd8\xff"):
		mime = "image/jpeg"
	elif content.startswith(b"\x89PNG\r\n\x1a\n"):
		mime = "image/png"
	elif content.startswith(b"%PDF-"):
		mime = "application/pdf"
	else:
		_fail("the source is not a supported JPEG, PNG or PDF file, or it exceeds 15 MB")
	key, model = _credentials()
	payload, payload_mime = _for_gemini(content, mime)
	extracted = _generate(payload, payload_mime, key, model)
	try:
		return document_from_extraction(extracted, content)
	except FacturaImportError as exc:
		detail = str(exc)
		retryable = (
			"item totals do not match" in detail
			or "item rows do not reconcile" in detail
			or "customer identity" in detail
		)
		if not retryable:
			raise
		extracted = _generate(
			payload, payload_mime, key, model, prompt=_correction_text(extracted, detail)
		)
		return document_from_extraction(extracted, content)
