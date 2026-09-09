"""Strict, local PDF import for the observed Orange and ARAX factura layouts.

No ERP writes, OCR, remote calls or signature-trust claims belong in this parser.
Unrecognised layouts and unreconciled totals fail closed for manual registration.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MAX_PDF_BYTES = 15 * 1024 * 1024
SCAN_TEXT_CHARS = 40
NUMBER = r"-?\d+(?:[.,]\d+)?"
DATE = r"\d{2}\.\d{2}\.\d{4}"
PROVIDERS = {
	"1003600106115": ("Orange", "I.M. Orange Moldova S.A."),
	"1002600041697": ("ARAX", "ARAX-IMPEX SRL"),
}


class FacturaImportError(ValueError):
	pass


def decimal(value) -> Decimal:
	try:
		result = Decimal(str(value).replace(",", "."))
	except (InvalidOperation, ValueError):
		raise FacturaImportError("Invalid numeric value") from None
	if not result.is_finite():
		raise FacturaImportError("Non-finite numeric value")
	return result


def money(value) -> Decimal:
	return decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _label_text(text):
	normalized = unicodedata.normalize("NFKD", (text or "").replace("\u00a0", " "))
	return "".join(c for c in normalized if not unicodedata.combining(c))


def _blob(text, layout=""):
	return f"{text or ''}\n{layout or ''}"


def _is_fiscal_invoice(text, layout=""):
	blob = _label_text(_blob(text, layout))
	if re.search(r"factur[ae]?\s*fiscal[ae]?", blob, re.IGNORECASE):
		return True
	if re.search(r"formular\s+tipizat", blob, re.IGNORECASE) and re.search(r"\b[A-Z]{3}\d{7}\b", blob):
		return True
	return any(idno in blob for idno in PROVIDERS)


def _capture(pattern, text, label):
	match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
	if not match:
		raise FacturaImportError(f"Cannot read {label}; register this factura manually")
	return match


def _date(value):
	try:
		return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
	except ValueError:
		raise FacturaImportError("Invalid factura date") from None


def _split_columns(line):
	return [part.strip() for part in re.split(r"\s{2,}", line.strip()) if part.strip()]


def _party_source_details(layout: str, provider: str) -> dict:
	"""Retain requisites printed on the supported originals without resolving ERP masters."""
	lines = layout.splitlines()
	if provider == "Orange":
		start = next((i for i, line in enumerate(lines) if "1. Furnizor:" in line), None)
		if start is None:
			return {}
		body = [_split_columns(line) for line in lines[start + 1 : start + 13]]
		body = [parts for parts in body if parts]
		if len(body) < 10:
			return {}
		supplier_address = ", ".join([body[1][0], body[2][0], body[3][0], body[4][0]])
		buyer_address = ", ".join([body[1][-1], body[3][-1], body[4][-1]])
		return {
			"supplier_name": body[0][0],
			"buyer_name": body[0][-1],
			"supplier_address": supplier_address,
			"buyer_address": buyer_address,
			"supplier_bank_account": body[7][0].removeprefix("Codul IBAN:").strip(),
			"supplier_bank_code": body[8][0].removeprefix("Codul bancii:").strip(),
			"supplier_bank_name": body[9][0],
			"buyer_bank_account": body[8][-1].removeprefix("Cont de plati:").strip(),
			"buyer_bank_code": body[9][-1].removeprefix("Codul bancii:").strip(),
			"buyer_bank_name": body[10][-1],
		}

	details = {}
	for party, label in (("supplier", "1. Furnizor:"), ("buyer", "2. Cumpărător / beneficiar:")):
		start = next((i for i, line in enumerate(lines) if label in line), None)
		if start is None or start + 1 >= len(lines):
			continue
		party_line = re.split(r"c\.f\./nr\.TVA", lines[start], maxsplit=1)[0].split(label, 1)[-1].strip()
		match = re.match(r"(.+?\b(?:SRL|SA)),\s*(.*?),?\s*$", party_line, re.IGNORECASE)
		bank = re.search(r"\b(MD[A-Z0-9]+)\s*,\s*([A-Z0-9]+)", "\n".join(lines[start : start + 3]))
		if match:
			details[f"{party}_name"] = match[1].strip()
			details[f"{party}_address"] = match[2].strip().rstrip(",")
		if bank:
			details[f"{party}_bank_account"] = bank[1]
			details[f"{party}_bank_code"] = bank[2]
	return details


def parse_text(text: str, layout: str) -> dict:
	"""Parse one supported page; layout preserves the original item/UOM columns."""
	source = _blob(text, layout)
	normalized = _label_text(source)
	if not _is_fiscal_invoice(text, layout):
		raise FacturaImportError("The PDF is not a recognised fiscal invoice")
	if "Orange Moldova" in source:
		pairs = vat_ids = []
		for raw in (text, layout, source):
			found_idno = re.findall(r"Cod fiscal:\s*(\d{13})", raw or "", re.IGNORECASE)
			found_vat = re.findall(r"Cod TVA:\s*(\d{7})", raw or "", re.IGNORECASE)
			if len(found_idno) == 2 and len(found_vat) == 2:
				pairs, vat_ids = found_idno, found_vat
				break
		if not pairs:
			pairs = re.findall(r"Cod fiscal:\s*(\d{13})", source, re.IGNORECASE)
			vat_ids = re.findall(r"Cod TVA:\s*(\d{7})", source, re.IGNORECASE)
		expected_idno = "1003600106115"
		series = _capture(r"Seria\s+([A-Z]+)", normalized, "series")[1]
		number = _capture(r"Numarul facturii\s+(\d+)", normalized, "number")[1]
		issued = _capture(r"Data eliberarii\s+(" + DATE + ")", normalized, "issue date")[1]
		delivered = _capture(r"Data livrarii\s+(" + DATE + ")", normalized, "delivery date")[1]
	else:
		pairs_vat = []
		for raw in (text, layout, source):
			found = re.findall(r"\b(\d{13})\s*/\s*(\d{7})\b", raw or "")
			if len(found) == 2:
				pairs_vat = found
				break
		if not pairs_vat:
			pairs_vat = re.findall(r"\b(\d{13})\s*/\s*(\d{7})\b", source)
		pairs = [p[0] for p in pairs_vat]
		vat_ids = [p[1] for p in pairs_vat]
		expected_idno = "1002600041697"
		ref = _capture(r"\b([A-Z]{3})(\d{7})\b", source, "series and number")
		series, number = ref[1], ref[2]
		dates = _capture(r"(" + DATE + r")\s*/\s*(" + DATE + ")", source, "issue/delivery dates")
		issued, delivered = dates[1], dates[2]
	if len(pairs) != 2 or pairs[0] != expected_idno or len(vat_ids) != 2:
		raise FacturaImportError("Unsupported provider or ambiguous issuer/recipient requisites")
	provider, supplier_name = PROVIDERS[expected_idno]
	result = {
		"provider": provider,
		"supplier_name": supplier_name,
		"supplier_idno": pairs[0],
		"supplier_vat_id": vat_ids[0],
		"buyer_idno": pairs[1],
		"buyer_vat_id": vat_ids[1],
		"series": series,
		"number": number,
		"issue_date": _date(issued),
		"delivery_date": _date(delivered),
		"currency": "MDL",
	}
	result.update(_party_source_details(layout, provider))
	if provider == "Orange":
		for key, pattern in (
			("provider_reference", r"Numarul de referin\w+\s+(\d+)"),
			("provider_account", r"Numarul contului\s+(\d+)"),
			("contract_reference", r"Nr\. Contractului:\s*([^\s]+)"),
		):
			match = re.search(pattern, normalized, re.IGNORECASE)
			if match:
				result[key] = match[1]
	else:
		act = re.search(r"Act\s*[№#]?\s*(\d+)\s+din\s+(" + DATE + ")", source, re.IGNORECASE)
		if act:
			result.update(related_document_number=act[1], related_document_date=_date(act[2]))
			result["related_document_type"] = "Act"

	items = []
	for line in layout.splitlines():
		parts = re.split(r"\s{2,}", line.strip())
		if re.fullmatch(NUMBER, parts[0]):
			continue
		if len(parts) not in (7, 8) or not all(re.fullmatch(NUMBER, p) for p in parts[-6:]):
			continue
		qty, rate, third, fourth, vat, gross = map(decimal, parts[-6:])
		net, vat_rate = (fourth, third) if provider == "Orange" else (third, fourth)
		if qty <= 0 or rate < 0 or net < 0 or vat_rate < 0 or vat_rate > 100:
			raise FacturaImportError("Returns/negative lines are not supported by this importer")
		if abs(money(qty * rate) - money(net)) > Decimal("0.01"):
			raise FacturaImportError("Item quantity, rate and net amount do not reconcile")
		if abs(money(net * vat_rate / 100) - money(vat)) > Decimal("0.01"):
			raise FacturaImportError("Item VAT does not reconcile")
		if money(net + vat) != money(gross):
			raise FacturaImportError("Item gross amount does not reconcile")
		items.append(
			{
				"description": parts[0],
				"source_uom": parts[1] if len(parts) == 8 else "",
				"source_qty": str(qty),
				"source_rate": str(rate),
				"net_amount": str(net),
				"vat_rate": str(vat_rate),
				"vat_amount": str(vat),
				"amount": str(gross),
				"qty": str(qty),
			}
		)
	if not items:
		raise FacturaImportError("No supported item rows found; register this factura manually")
	total_line = _capture(r"^\s*12\.\s*TOTAL[^\n]+", layout, "invoice totals")[0]
	values = re.findall(NUMBER, total_line.split(")", 1)[-1])
	if len(values) != 3:
		raise FacturaImportError("Cannot read invoice net/VAT/gross totals")
	for field, item_field, value in zip(
		("net_total", "vat_total", "total"),
		("net_amount", "vat_amount", "amount"),
		values,
		strict=True,
	):
		if money(sum(decimal(row[item_field]) for row in items)) != money(value):
			raise FacturaImportError("Extracted rows do not reconcile with invoice totals")
		result[field] = str(money(value))
	result["items"] = items
	return result


def is_pdf_image_scan(content: bytes) -> bool:
	"""True when the PDF has no usable text layer (a photograph or image-only scan)."""
	from pypdf import PdfReader

	if not content.startswith(b"%PDF-"):
		return False
	try:
		reader = PdfReader(io.BytesIO(content))
		if reader.is_encrypted:
			raise FacturaImportError("Unable to read this PDF; register it manually")
		chars = 0
		has_font = False
		for page in reader.pages:
			text = page.extract_text() or ""
			chars += len(re.sub(r"\W+", "", text, flags=re.UNICODE))
			resources = page.get("/Resources")
			if resources is not None:
				res = resources.get_object() if hasattr(resources, "get_object") else resources
				if res.get("/Font"):
					has_font = True
			if chars >= SCAN_TEXT_CHARS:
				return False
	except FacturaImportError:
		raise
	except Exception as exc:
		raise FacturaImportError("Unable to read this PDF; register it manually") from exc
	return not has_font


def parse_pdf(content: bytes) -> dict:
	from pypdf import PdfReader

	if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
		raise FacturaImportError("Upload a PDF smaller than 15 MB")
	try:
		reader = PdfReader(io.BytesIO(content))
		if reader.is_encrypted or len(reader.pages) != 1:
			raise FacturaImportError("Only unencrypted, single-page Orange/ARAX PDFs are supported")
		page = reader.pages[0]
		text = page.extract_text() or ""
		result = parse_text(text, page.extract_text(extraction_mode="layout") or "")
		from erpnext_moldova_efactura.utils.factura_pdf_signature import (
			signatures_from_reader,
			summarize_signatures,
		)

		signatures = signatures_from_reader(reader, content)
		inspection = summarize_signatures(signatures, verified=False) if signatures else None
	except FacturaImportError:
		raise
	except Exception as exc:
		raise FacturaImportError("Unable to read this PDF; register it manually") from exc
	result.update(
		original_format="Digitally Signed PDF" if signatures else "Other Electronic",
		signature_status="Not Checked" if signatures else "Not Applicable",
		file_hash=hashlib.sha256(content).hexdigest(),
	)
	if inspection:
		for key in (
			"signature_format",
			"signature_field",
			"signature_declared_time",
			"signature_coverage",
			"signature_integrity",
			"signature_certificate_trust",
			"signature_revocation",
			"signature_timestamp_check",
			"signature_evidence",
		):
			result[key] = inspection[key]
	return result
