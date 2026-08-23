"""Sync outgoing e-Factura invoices (ActorRole=1) issued outside ERP into Sales eFactura."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import extract_invoices, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import (
	SFS_ARCHIVED,
	do_not_create_cancelled_invoices,
	is_canceled_by_supplier,
	load_archived_sales_efactura,
)
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml
from erpnext_moldova_efactura.utils.party import find_customer_by_idno, get_default_company

DEFAULT_LOOKBACK_DAYS = 180
BATCH_DETAIL = 100

# Draft through Cancellation Requested (supplier inbox). Archived (6) is optional.
SUPPLIER_SEARCH_STATUSES = (0, 1, 2, 3, 5, 7, 8, 9, 10, 11)


def supplier_search_statuses() -> tuple[int, ...]:
	if load_archived_sales_efactura():
		return SUPPLIER_SEARCH_STATUSES + (SFS_ARCHIVED,)
	return SUPPLIER_SEARCH_STATUSES


def sync_supplier_invoices(lookback_days: int | None = None, company: str | None = None) -> dict:
	"""Search supplier invoices and upsert Sales eFactura docs that are missing locally."""
	client = EFacturaAPIClient.from_settings()
	lookback_days = int(lookback_days or DEFAULT_LOOKBACK_DAYS)
	company = company or get_default_company()
	if not company:
		frappe.throw(_("No Company found for Sales eFactura sync"))

	date_from = add_days(now_datetime(), -lookback_days)
	date_to = now_datetime()

	seen: dict[tuple[str, str], int] = {}
	for st in supplier_search_statuses():
		params = {
			"InvoiceStatus": st,
			"IssuedOn": {"StartDate": date_from, "EndDate": date_to},
		}
		try:
			resp = client.search_invoices(actor_role=1, parameters=params)
		except Exception:
			frappe.log_error(
				title=f"Sales eFactura SearchInvoices failed status={st}",
				message=frappe.get_traceback(),
			)
			continue

		for inv in extract_invoices(resp):
			seria = str(inv.get("Seria") or "").strip()
			number = str(inv.get("Number") or "").strip()
			if not seria or not number:
				continue
			try:
				code = int(inv.get("InvoiceStatus"))
			except (TypeError, ValueError):
				code = st
			seen[(seria, number)] = code

	created = updated = skipped = details = errors = 0
	skip_cancelled = do_not_create_cancelled_invoices()
	bank = _default_company_bank(company)

	for (seria, number), ef_status in seen.items():
		try:
			name = frappe.db.exists(
				"Sales eFactura",
				{"company": company, "ef_series": seria, "ef_number": number},
			)
			if name:
				doc = frappe.get_doc("Sales eFactura", name)
				if cint(doc.ef_status) != cint(ef_status):
					doc.db_set("ef_status", ef_status, update_modified=False)
					doc.set_status()
					updated += 1
				doc.db_set("last_status_check", now_datetime(), update_modified=False)
				if _fill_details(client, doc.name):
					details += 1
				continue

			if skip_cancelled and is_canceled_by_supplier(ef_status):
				skipped += 1
				continue

			xml = _fetch_xml(client, seria, number)
			parsed = parse_invoice_xml(xml) if xml else {}
			customer = find_customer_by_idno((parsed.get("buyer") or {}).get("idno"))
			doc_type = "Non-Transfer" if str(parsed.get("creation_motiv") or "") == "5" else "Transfer"
			doc = frappe.get_doc(
				{
					"doctype": "Sales eFactura",
					"naming_series": (
						"ACC-SEF-NT-.YYYY.-" if doc_type == "Non-Transfer" else "ACC-SEF-.YYYY.-"
					),
					"company": company,
					"type": doc_type,
					"ef_series": seria,
					"ef_number": number,
					"ef_status": ef_status,
					"company_bank_account": bank,
					"customer": customer,
					"last_status_check": now_datetime(),
				}
			)
			doc.flags.from_efactura_sync = True
			doc.flags.ignore_si_qty_guard = True
			doc.flags.ignore_mandatory = True
			if xml:
				doc.fill_from_xml(xml)
			doc.insert(ignore_permissions=True, ignore_mandatory=True)
			created += 1
			if xml:
				details += 1
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"Sales eFactura upsert failed {seria}{number}",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()
	return {
		"found": len(seen),
		"created": created,
		"updated": updated,
		"skipped": skipped,
		"details_loaded": details,
		"errors": errors,
	}


def _fetch_xml(client: EFacturaAPIClient, seria: str, number: str) -> str | None:
	resp = client.get_invoices_by_seria_number([{"Seria": seria, "Number": number}])
	invs = extract_invoices(resp)
	if not invs:
		return None
	return invoice_xml(invs[0])


def _fill_details(client: EFacturaAPIClient, name: str) -> bool:
	doc = frappe.get_doc("Sales eFactura", name)
	if cint(doc.docstatus) != 0:
		return False
	has_items = bool(doc.items)
	has_vat = flt(doc.vat_total) or flt(doc.ef_vat_total)
	if has_items and has_vat:
		return False
	xml = _fetch_xml(client, doc.ef_series, doc.ef_number)
	if not xml:
		return False
	if has_items:
		parsed = parse_invoice_xml(xml)
		if not flt(parsed.get("vat_total")):
			return False
	doc.flags.from_efactura_sync = True
	doc.flags.ignore_si_qty_guard = True
	doc.flags.keep_xml_amounts = True
	doc.fill_from_xml(xml)
	doc.save(ignore_permissions=True, ignore_mandatory=True)
	return True


def _default_company_bank(company: str) -> str | None:
	name = frappe.db.get_value(
		"Bank Account",
		{"company": company, "is_company_account": 1, "is_default": 1},
		"name",
	)
	if name:
		return name
	return frappe.db.get_value(
		"Bank Account",
		{"company": company, "is_company_account": 1},
		"name",
	)


@frappe.whitelist()
def fetch_supplier_invoices(lookback_days: int = 180):
	if not frappe.has_permission("Sales eFactura", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return sync_supplier_invoices(lookback_days=int(lookback_days))
