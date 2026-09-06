"""Original factura amounts and the same currency/UOM conventions as PEF."""

from types import SimpleNamespace

import frappe
from frappe import _
from frappe.utils import flt, getdate

from erpnext_moldova_efactura.utils.factura_pdf import decimal
from erpnext_moldova_efactura.utils.pef_currency import (
	apply_document_amounts_from_ef,
	apply_ef_conversion_rate_rules,
	default_document_currency,
	settings_ef_currency,
)
from erpnext_moldova_efactura.utils.uom_map import apply_qty_defaults


def prepare_currency(doc):
	doc.currency = doc.currency or default_document_currency(doc.supplier_party, doc.company)
	doc.f_currency = doc.f_currency or settings_ef_currency()
	previous = doc.get_doc_before_save()
	if previous and (
		any(doc.get(k) != previous.get(k) for k in ("currency", "f_currency"))
		or getdate(doc.issue_date) != getdate(previous.issue_date)
	):
		if flt(doc.f_conversion_rate) == flt(previous.f_conversion_rate):
			doc.f_conversion_rate = 0
	proxy = SimpleNamespace(
		currency=doc.currency,
		ef_currency=doc.f_currency,
		ef_conversion_rate=doc.f_conversion_rate,
		issue_date=doc.issue_date,
	)
	apply_ef_conversion_rate_rules(proxy)
	doc.f_conversion_rate = proxy.ef_conversion_rate
	if decimal(doc.f_conversion_rate or 0) <= 0:
		frappe.throw(_("Set a positive Factura Exchange Rate for the selected currencies"))


def apply_quantities(doc, row):
	previous_doc = doc.get_doc_before_save()
	previous = next((r for r in previous_doc.items if r.name == row.name), None) if previous_doc else None
	changed = not previous or any(row.get(k) != previous.get(k) for k in ("item_code", "f_uom", "uom"))
	if previous and not changed:
		# Factors belong to the reviewed mapping, not to later changes in Item master data.
		row.conversion_factor = previous.conversion_factor
		row.f_conversion_factor = previous.f_conversion_factor
	proxy = SimpleNamespace(**row.as_dict())
	for key in ("uom", "qty", "conversion_factor"):
		setattr(proxy, "ef_" + key, row.get("f_" + key))
	apply_qty_defaults(proxy, force=changed)
	for key in ("uom", "qty", "conversion_factor"):
		row.set("f_" + key, getattr(proxy, "ef_" + key))
	for key in ("uom", "qty", "conversion_factor", "stock_uom", "stock_qty"):
		row.set(key, getattr(proxy, key))
	if row.item_code:
		from erpnext.stock.get_item_details import get_conversion_factor

		if changed:
			for uom_field in ("uom", "f_uom"):
				conversion = get_conversion_factor(row.item_code, row.get(uom_field)) or {}
				if flt(conversion.get("conversion_factor")) <= 0:
					frappe.throw(
						_("Row {0}: configure the Item conversion for UOM {1}").format(
							row.idx, row.get(uom_field)
						)
					)
		if flt(row.qty) <= 0:
			frappe.throw(_("Row {0}: Quantity must be positive").format(row.idx))
		row.item_name = frappe.get_cached_value("Item", row.item_code, "item_name")


def convert_amounts(doc):
	proxy = tax_source(doc)
	proxy.ef_currency = doc.f_currency
	proxy.ef_conversion_rate = doc.f_conversion_rate
	proxy.issue_date = doc.issue_date
	proxy.get = lambda key: getattr(proxy, key, None)
	for key in ("net_total", "vat_total", "total"):
		setattr(proxy, "ef_" + key, doc.get("f_" + key))
	apply_document_amounts_from_ef(proxy)
	for source, row in zip(proxy.items, doc.items, strict=True):
		for key in ("rate", "rate_with_vat", "amount", "net_amount", "vat_amount"):
			row.set(key, getattr(source, key, 0))
	for key in ("net_total", "vat_total", "total"):
		doc.set(key, getattr(proxy, key))
	if not flt(doc.f_total):
		# The shared PEF helper skips empty totals; clear derived amounts after a zero-price edit.
		for row in doc.items:
			for field in ("rate", "rate_with_vat", "amount", "net_amount", "vat_amount"):
				row.set(field, 0)
		doc.net_total = doc.vat_total = doc.total = 0


def imported_fields(data):
	"""The PDF parser remains independent of ERP DocType field names."""
	for old, new in (
		("series", "f_series"),
		("number", "f_number"),
		("supplier_idno", "f_supplier_idno"),
		("supplier_name", "f_supplier_name"),
		("supplier_vat_id", "f_supplier_vat_id"),
		("supplier_address", "f_supplier_address"),
		("supplier_bank_account", "f_supplier_bank_account"),
		("supplier_bank_name", "f_supplier_bank_name"),
		("supplier_bank_code", "f_supplier_bank_code"),
		("buyer_idno", "f_customer_idno"),
		("buyer_name", "f_customer_name"),
		("buyer_vat_id", "f_customer_vat_id"),
		("buyer_address", "f_customer_address"),
		("buyer_bank_account", "f_customer_bank_account"),
		("buyer_bank_name", "f_customer_bank_name"),
		("buyer_bank_code", "f_customer_bank_code"),
		("provider_reference", "f_provider_reference"),
		("provider_account", "f_provider_account"),
		("contract_reference", "f_contract_reference"),
		("service_period_start", "f_service_period_start"),
		("service_period_end", "f_service_period_end"),
		("related_document_type", "f_related_document_type"),
		("related_document_number", "f_related_document_number"),
		("related_document_date", "f_related_document_date"),
	):
		if old in data:
			data[new] = data.pop(old)
	data["f_currency"] = data.pop("currency")
	for name in ("net_total", "vat_total", "total"):
		data["f_" + name] = data.pop(name)
	for row in data["items"]:
		for old, new in (
			("description", "supplier_item_name"),
			("source_uom", "supplier_uom"),
			("source_qty", "f_qty"),
			("source_rate", "f_rate"),
			("vat_rate", "f_vat_rate"),
			("net_amount", "f_net_amount"),
			("vat_amount", "f_vat_amount"),
			("amount", "f_amount"),
		):
			row[new] = row.pop(old)
	return data


def tax_source(doc):
	"""Adapt PF fields for shared PEF helpers without persisting XML field names on PF."""
	rows = []
	for row in doc.items:
		values = row.as_dict()
		values.update(
			{"ef_" + key[2:]: value for key, value in row.as_dict().items() if key.startswith("f_")}
		)
		rows.append(SimpleNamespace(**values))
	return SimpleNamespace(
		currency=doc.currency, net_total=doc.net_total, vat_total=doc.vat_total, total=doc.total, items=rows
	)
