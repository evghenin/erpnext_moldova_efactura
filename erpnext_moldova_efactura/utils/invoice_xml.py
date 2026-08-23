"""Parse e-Factura invoice XML (SupplierInfo / Merchandises)."""

from __future__ import annotations

import html
from typing import Any
from xml.etree import ElementTree as ET

from frappe.utils import flt, get_datetime, get_time, getdate

from erpnext_moldova_efactura.utils.taxpayer_type import taxpayer_type_from_sfs


def parse_sfs_amount(value) -> float:
	"""Parse SFS money/qty. flt() strips commas, so '97,81' must not become 9781."""
	if value is None or value == "":
		return 0.0
	if isinstance(value, int | float):
		return flt(value)
	text = unescape_sfs_text(str(value)).replace("\xa0", "").replace(" ", "")
	if not text:
		return 0.0
	if "," in text and "." in text:
		if text.rfind(",") > text.rfind("."):
			text = text.replace(".", "").replace(",", ".")
		else:
			text = text.replace(",", "")
	elif "," in text:
		text = _single_separator_to_dot(text, ",")
	elif text.count(".") == 1:
		text = _single_separator_to_dot(text, ".")
	return flt(text)


def _single_separator_to_dot(text: str, sep: str) -> str:
	left, right = text.split(sep, 1)
	if right.isdigit() and 1 <= len(right) <= 2:
		return f"{left.replace('.', '').replace(',', '')}.{right}"
	return text.replace(sep, "")


def unescape_sfs_text(value: str | None) -> str:
	"""Decode HTML entities; SFS often double-encodes (&amp;apos; → &apos; → ')."""
	if value is None:
		return ""
	if not isinstance(value, str):
		value = str(value)
	if not value:
		return value
	prev = None
	cur = value
	for _ in range(3):
		if prev == cur:
			break
		prev = cur
		cur = html.unescape(cur)
	return cur.strip()


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1] if tag else tag


def _attr(el: ET.Element | None, name: str, default: str = "") -> str:
	if el is None:
		return default
	return unescape_sfs_text(el.attrib.get(name) or default)


def _text(parent: ET.Element | None, child: str, default: str = "") -> str:
	if parent is None:
		return default
	for el in list(parent):
		if _local(el.tag) == child:
			return unescape_sfs_text(el.text or default)
	return default


def _find(parent: ET.Element | None, name: str) -> ET.Element | None:
	if parent is None:
		return None
	for el in parent.iter():
		if _local(el.tag) == name:
			return el
	return None


def _party_block(supplier_info: ET.Element, party_tag: str) -> dict[str, str]:
	party = None
	for el in list(supplier_info):
		if _local(el.tag) == party_tag:
			party = el
			break
	bank = _find(party, "BankAccount") if party is not None else None
	return {
		"idno": _attr(party, "IDNO"),
		"vat_id": _attr(party, "CodTVA"),
		"taxpayer_type": taxpayer_type_from_sfs(_attr(party, "TaxpayerType")),
		"name": _attr(party, "Title"),
		"address": _attr(party, "Address"),
		"bank_account": _attr(bank, "Account"),
		"bank_name": _attr(bank, "BranchTitle"),
		"bank_code": _attr(bank, "BranchCode"),
	}


def _safe_date(value: str):
	if not value:
		return None
	try:
		return getdate(value[:10])
	except Exception:
		return None


def _safe_time(value: str):
	"""Time from SFS ISO timestamps, including fractional seconds and Z."""
	if not value:
		return None
	raw = str(value).strip()
	try:
		normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
		dt = get_datetime(normalized)
		if dt:
			return get_time(dt).replace(microsecond=0)
	except Exception:
		pass
	try:
		if "T" in raw:
			part = raw.split("T", 1)[1]
		elif " " in raw:
			part = raw.split(" ", 1)[1]
		else:
			part = raw
		part = part.replace("Z", "").split("+", 1)[0]
		hhmmss = part.split(".", 1)[0]
		return get_time(hhmmss[:8]).replace(microsecond=0)
	except Exception:
		return None


def _row_vat_rate(row: ET.Element, net_amount: float, vat_amount: float) -> float:
	"""TVA percent from the row attribute, or inferred from TotalTVA / net."""
	for name in ("TVA", "Vat", "VAT", "ProcTVA"):
		raw = _attr(row, name)
		if raw:
			rate = parse_sfs_amount(raw)
			if rate:
				return rate
	if net_amount and vat_amount:
		return flt(round((vat_amount / net_amount) * 100.0))
	return 0.0


def parse_invoice_xml(xml_content: str | bytes) -> dict[str, Any]:
	"""Return structured invoice data from e-Factura XML."""
	if isinstance(xml_content, bytes):
		xml_content = xml_content.decode("utf-8", errors="ignore")

	root = ET.fromstring(xml_content)
	supplier_info = _find(root, "SupplierInfo") or root

	supplier = _party_block(supplier_info, "Supplier")
	buyer = _party_block(supplier_info, "Buyer")
	transporter = _party_block(supplier_info, "Transporter")

	total = parse_sfs_amount(_text(supplier_info, "Total"))
	vat_total = parse_sfs_amount(_text(supplier_info, "TotalTVA"))

	items: list[dict[str, Any]] = []
	merchandises = _find(supplier_info, "Merchandises")
	if merchandises is not None:
		for row in merchandises:
			if _local(row.tag) != "Row":
				continue
			name = _attr(row, "Name") or _attr(row, "Code") or "Item"
			qty = parse_sfs_amount(_attr(row, "Quantity"))
			rate = parse_sfs_amount(_attr(row, "UnitPriceWithoutTVA"))
			amount = parse_sfs_amount(_attr(row, "TotalPrice"))
			net_amount = parse_sfs_amount(_attr(row, "TotalPriceWithoutTVA"))
			vat_amount = parse_sfs_amount(_attr(row, "TotalTVA"))
			vat_rate = _row_vat_rate(row, net_amount, vat_amount)
			if qty:
				rate_with_vat = flt(amount / qty)
			elif vat_rate:
				rate_with_vat = flt(rate * (1.0 + vat_rate / 100.0))
			else:
				rate_with_vat = rate
			supplier_uom = (_attr(row, "UnitOfMeasure") or "")[:140]
			items.append(
				{
					"supplier_item_code": (_attr(row, "Code") or "")[:140],
					"supplier_item_name": name[:1000],
					"supplier_uom": supplier_uom,
					"ef_qty": qty,
					"qty": qty,
					"stock_qty": qty,
					"rate": rate,
					"rate_with_vat": rate_with_vat,
					"net_amount": net_amount,
					"ef_vat_rate": vat_rate,
					"vat_amount": vat_amount,
					"amount": amount,
				}
			)

	return {
		"ef_series": _text(supplier_info, "Seria"),
		"ef_number": _text(supplier_info, "Number"),
		"issue_date": _safe_date(_text(supplier_info, "IssuedDate")),
		"issue_time": _safe_time(_text(supplier_info, "IssuedDate")),
		"delivery_date": _safe_date(_text(supplier_info, "DeliveryDate")),
		"creation_motiv": _text(supplier_info, "CreationMotiv"),
		"total": total,
		"vat_total": vat_total,
		"net_total": flt(total - vat_total),
		"supplier": supplier,
		"buyer": buyer,
		"transporter": transporter,
		"items": items,
	}
