"""Sync incoming e-Factura invoices (ActorRole=2) into eFactura Buyer."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import extract_invoices, invoice_status_map, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import BUYER_SEARCH_STATUSES, status_label
from erpnext_moldova_efactura.utils.party import find_supplier_by_idno, get_default_company

DEFAULT_LOOKBACK_DAYS = 180
BATCH_DETAIL = 100


def sync_buyer_invoices(lookback_days: int | None = None, company: str | None = None) -> dict:
	"""Search buyer invoices and upsert eFactura Buyer docs. Read + local write only."""
	client = EFacturaAPIClient.from_settings()
	lookback_days = int(lookback_days or DEFAULT_LOOKBACK_DAYS)
	company = company or get_default_company()
	if not company:
		frappe.throw("No Company found for eFactura Buyer sync")

	date_from = add_days(now_datetime(), -lookback_days)
	date_to = now_datetime()

	seen: dict[tuple[str, str], int] = {}
	for st in BUYER_SEARCH_STATUSES:
		params = {
			"InvoiceStatus": st,
			"IssuedOn": {"StartDate": date_from, "EndDate": date_to},
		}
		try:
			resp = client.search_invoices(actor_role=2, parameters=params)
		except Exception:
			frappe.log_error(
				title=f"eFactura Buyer SearchInvoices failed status={st}",
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

	created = updated = details = errors = 0
	# Fetch XML details for a limited batch (prefer docs missing items/xml)
	detail_candidates = []

	for (seria, number), ef_status in seen.items():
		try:
			name = frappe.db.exists(
				"eFactura Buyer",
				{"company": company, "ef_series": seria, "ef_number": number},
			)
			if name:
				doc = frappe.get_doc("eFactura Buyer", name)
				doc.ef_status = ef_status
				doc.last_status_check = now_datetime()
				doc.set_status(update=False)
				doc.save(ignore_permissions=True)
				updated += 1
				# Details come from SFS on demand; refill if parties/items missing
				if not doc.items or not doc.ef_supplier_idno:
					detail_candidates.append(doc.name)
			else:
				doc = frappe.get_doc(
					{
						"doctype": "eFactura Buyer",
						"naming_series": "EFB-.YYYY.-",
						"company": company,
						"ef_series": seria,
						"ef_number": number,
						"ef_status": ef_status,
						"status": status_label(ef_status),
						"last_status_check": now_datetime(),
						"currency": frappe.db.get_single_value("eFactura Settings", "currency")
						or "MDL",
					}
				)
				doc.flags.from_efactura_sync = True
				doc.insert(ignore_permissions=True)
				created += 1
				detail_candidates.append(doc.name)
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"eFactura Buyer upsert failed {seria}{number}",
				message=frappe.get_traceback(),
			)

	for name in detail_candidates[:BATCH_DETAIL]:
		try:
			if _fill_details(client, name):
				details += 1
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"eFactura Buyer detail fetch failed {name}",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()
	return {
		"found": len(seen),
		"created": created,
		"updated": updated,
		"details_loaded": details,
		"errors": errors,
	}


def _fill_details(client: EFacturaAPIClient, name: str) -> bool:
	doc = frappe.get_doc("eFactura Buyer", name)
	# Never rewrite lines on submitted docs
	if doc.docstatus != 0:
		return False

	resp = client.get_invoices_by_seria_number(
		[{"Seria": doc.ef_series, "Number": doc.ef_number}]
	)
	invs = extract_invoices(resp)
	if not invs:
		return False

	inv = invs[0]
	if inv.get("InvoiceStatus") is not None:
		doc.ef_status = int(inv.get("InvoiceStatus"))

	xml = invoice_xml(inv)
	if not xml:
		doc.last_status_check = now_datetime()
		doc.set_status(update=False)
		doc.save(ignore_permissions=True)
		return False

	preserve_supplier = doc.supplier
	doc.flags.allow_sfs_item_refresh = True
	doc.fill_from_xml(xml, preserve_mapped_items=True)
	if preserve_supplier:
		doc.supplier = preserve_supplier
	elif doc.ef_supplier_idno and not doc.supplier:
		doc.supplier = find_supplier_by_idno(doc.ef_supplier_idno)

	doc.last_status_check = now_datetime()
	doc.set_status(update=False)
	doc.save(ignore_permissions=True)
	return True


def sync_buyer_statuses(batch_size: int = 50) -> dict:
	"""Refresh ef_status for existing buyer docs via CheckInvoicesStatus."""
	rows = frappe.get_all(
		"eFactura Buyer",
		fields=["name", "ef_series", "ef_number", "ef_status"],
		filters={"ef_series": ["is", "set"], "ef_number": ["is", "set"]},
		order_by="modified asc",
		limit_page_length=batch_size,
	)
	if not rows:
		return {"checked": 0, "updated": 0}

	client = EFacturaAPIClient.from_settings()
	payload = [{"Seria": r.ef_series, "Number": r.ef_number} for r in rows]
	try:
		resp = client.check_invoices_status(seria_and_numbers=payload)
	except Exception:
		frappe.log_error(title="eFactura Buyer status sync failed", message=frappe.get_traceback())
		return {"checked": 0, "updated": 0, "error": 1}

	statuses = invoice_status_map(resp)
	updated = 0
	now_ts = now_datetime()
	for row in rows:
		key = (str(row.ef_series), str(row.ef_number))
		new_status = statuses.get(key)
		doc = frappe.get_doc("eFactura Buyer", row.name)
		if new_status is not None and doc.ef_status != new_status:
			doc.ef_status = new_status
			updated += 1
		doc.last_status_check = now_ts
		doc.set_status(update=False)
		doc.save(ignore_permissions=True)

	frappe.db.commit()
	return {"checked": len(rows), "updated": updated}


@frappe.whitelist()
def fetch_buyer_invoices(lookback_days: int = 180):
	frappe.only_for(("System Manager", "Accounts Manager"))
	return sync_buyer_invoices(lookback_days=int(lookback_days))
