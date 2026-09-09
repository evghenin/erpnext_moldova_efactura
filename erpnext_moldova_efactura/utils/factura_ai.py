"""Gemini vision import for photographed Moldovan fiscal invoices.

The original file stays in ERPNext. A copy of the image is sent to Google Gemini
for structured extraction. A draft is created only when identity, parties, every
item and totals reconcile locally.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from erpnext_moldova_efactura.utils.factura_pdf import FacturaImportError, decimal, money

MAX_IMAGE_BYTES = 15 * 1024 * 1024
OCR_ERROR = "Cannot read factura"
OCR_GUIDANCE = "Please provide a clearer, correctly oriented photo or scan of the complete document."

MAX_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE_HTTP = {429, 503}
RETRY_WAIT_SECONDS = (2, 5, 10)
RETIRED_MODELS = {
	"gemini-2.0-flash": DEFAULT_MODEL,
	"gemini-2.5-flash": DEFAULT_MODEL,
	"gemini-2.5-flash-lite": DEFAULT_MODEL,
}
PROMPT = """Extract this Moldovan fiscal invoice (factură fiscală) as JSON.

Rules:
- Read every charged item row once. Do not omit rows, invent rows, duplicate a numbered line, or guess missing numbers.
- Ignore column headers, the 12. TOTAL row, Total pagina, Total cantitate, Total greutate, weight, PL/PA notes, discounts of 0, and cashier/till labels.
- net_total is Val. tot. fara TVA / Valoarea fara TVA for the whole factura, never Total pagina.
- vat_total and total are the document VAT and payable gross. Do not use Total pagina as net_total or as the payable total.
- Fiscal identity is SERIA + NR (for example AAQ and 1838180). Bon fiscal / act / account stay related_document, not the factura number.
- source_qty is Cantitate. source_rate is unit price (Pret unitar / Pretul unitar), not pack price.
- source_uom is the printed packing/UOM (Mod amb., buc, serv). supplier_item_code is Cod articol/EAN when printed.
- Amounts use a dot decimal separator. VAT rates are 0, 5, 8 or 20.
- IDNO is 13 digits starting with 1. VAT ID is 7 digits when printed; leave blank if absent. Do not invent VAT IDs.
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


def _item(raw: dict) -> dict | None:
	try:
		qty = decimal(_blank(raw.get("source_qty")).replace(",", "."))
		rate = decimal(_blank(raw.get("source_rate")).replace(",", "."))
		net = money(_blank(raw.get("net_amount")).replace(",", "."))
		vat_rate = decimal(_blank(raw.get("vat_rate")).replace(",", ".").replace("%", ""))
		vat = money(_blank(raw.get("vat_amount")).replace(",", "."))
		gross = money(_blank(raw.get("amount")).replace(",", "."))
	except FacturaImportError:
		return None
	name = _blank(raw.get("description"))
	if len(name) < 3 or qty <= 0 or rate < 0 or net < 0 or vat < 0 or gross <= 0:
		return None
	if vat_rate not in (decimal("0"), decimal("5"), decimal("8"), decimal("20")):
		return None
	if abs(money(qty * rate) - net) > decimal("0.01"):
		return None
	if money(net + vat) != gross:
		return None
	# Till facturas (METRO) print VAT/gross after pack/promo rounding; trust printed VAT
	# when it stays within 5 bani of net * rate and still adds to gross.
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
	return None


def document_from_extraction(data: dict, content: bytes) -> dict:
	"""Validate Gemini JSON against factura arithmetic. Used by parse_image and tests."""
	if not isinstance(data, dict):
		_fail("the AI response was not a factura object")
	series = re.sub(r"[^A-Z]", "", _blank(data.get("series")).upper())
	number = _digits(_blank(data.get("number")))
	issue_date = _date(_blank(data.get("issue_date")))
	delivery_date = _date(_blank(data.get("delivery_date"))) or issue_date
	supplier_idno = _digits(_blank(data.get("supplier_idno")))
	buyer_idno = _digits(_blank(data.get("buyer_idno")))
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
	if not items:
		missing.append("complete item rows")
	if missing:
		_fail("could not extract " + ", ".join(missing))
	if len(items) != len(data.get("items") or []):
		_fail("one or more item rows do not reconcile")
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
		"supplier_vat_id": _digits(_blank(data.get("supplier_vat_id"))) or None,
		"supplier_address": _blank(data.get("supplier_address")) or None,
		"supplier_bank_account": _iban(_blank(data.get("supplier_bank_account"))) or None,
		"supplier_bank_name": _blank(data.get("supplier_bank_name")) or None,
		"supplier_bank_code": _blank(data.get("supplier_bank_code")) or None,
		"buyer_name": _blank(data.get("buyer_name")),
		"buyer_idno": buyer_idno,
		"buyer_vat_id": _digits(_blank(data.get("buyer_vat_id"))) or None,
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


def _generate(content: bytes, mime: str, key: str, model: str) -> dict:
	body = json.dumps(
		{
			"contents": [
				{
					"parts": [
						{"text": PROMPT},
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
			with urllib.request.urlopen(request, timeout=120) as response:
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
	return document_from_extraction(_generate(content, mime, key, model), content)
