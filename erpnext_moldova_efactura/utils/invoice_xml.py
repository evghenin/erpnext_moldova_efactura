"""Parse e-Factura invoice XML (SupplierInfo / Merchandises)."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from frappe.utils import flt, getdate


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1] if tag else tag


def _attr(el: ET.Element | None, name: str, default: str = "") -> str:
	if el is None:
		return default
	return (el.attrib.get(name) or default).strip()


def _text(parent: ET.Element | None, child: str, default: str = "") -> str:
	if parent is None:
		return default
	for el in list(parent):
		if _local(el.tag) == child:
			return (el.text or default).strip()
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
		"taxpayer_type": _attr(party, "TaxpayerType"),
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


def parse_invoice_xml(xml_content: str | bytes) -> dict[str, Any]:
	"""Return structured invoice data from e-Factura XML."""
	if isinstance(xml_content, bytes):
		xml_content = xml_content.decode("utf-8", errors="ignore")

	root = ET.fromstring(xml_content)
	supplier_info = _find(root, "SupplierInfo") or root

	supplier = _party_block(supplier_info, "Supplier")
	buyer = _party_block(supplier_info, "Buyer")
	transporter = _party_block(supplier_info, "Transporter")

	total = flt(_text(supplier_info, "Total") or 0)
	vat_total = flt(_text(supplier_info, "TotalTVA") or 0)

	items: list[dict[str, Any]] = []
	merchandises = _find(supplier_info, "Merchandises")
	if merchandises is not None:
		for row in merchandises:
			if _local(row.tag) != "Row":
				continue
			name = _attr(row, "Name") or _attr(row, "Code") or "Item"
			qty = flt(_attr(row, "Quantity") or 0)
			rate = flt(_attr(row, "UnitPriceWithoutTVA") or 0)
			amount = flt(_attr(row, "TotalPrice") or 0)
			vat_rate = flt(_attr(row, "TVA") or 0)
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
					"net_amount": flt(_attr(row, "TotalPriceWithoutTVA") or 0),
					"ef_vat_rate": vat_rate,
					"vat_amount": flt(_attr(row, "TotalTVA") or 0),
					"amount": amount,
				}
			)

	return {
		"ef_series": _text(supplier_info, "Seria"),
		"ef_number": _text(supplier_info, "Number"),
		"issue_date": _safe_date(_text(supplier_info, "IssuedDate")),
		"delivery_date": _safe_date(_text(supplier_info, "DeliveryDate")),
		"total": total,
		"vat_total": vat_total,
		"net_total": flt(total - vat_total),
		"supplier": supplier,
		"buyer": buyer,
		"transporter": transporter,
		"items": items,
	}
