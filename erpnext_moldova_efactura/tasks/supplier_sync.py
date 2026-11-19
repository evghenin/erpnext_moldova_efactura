"""Sync outgoing e-Factura invoices (ActorRole=1) issued outside ERP into Sales eFactura."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.tasks.schedule import SEARCH_READ_TIMEOUT
from erpnext_moldova_efactura.utils.api_response import (
	check_invoices_status_resilient,
	extract_invoices,
	invoice_xml,
)
from erpnext_moldova_efactura.utils.buyer_status import (
	SFS_ARCHIVED,
	do_not_create_cancelled_invoices,
	is_canceled_by_supplier,
	load_archived_sales_efactura,
)
from erpnext_moldova_efactura.utils.company_api import get_sync_targets
from erpnext_moldova_efactura.utils.fiscal_status import SEF_PENDING_REGISTRATION, sef_status_label
from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml
from erpnext_moldova_efactura.utils.party import find_customer_by_idno, find_supplier_by_idno
from erpnext_moldova_efactura.utils.search_windows import iter_search_invoices

DEFAULT_LOOKBACK_DAYS = 180
BATCH_DETAIL = 100

# Draft through Cancellation Requested (supplier inbox). Archived (6) is optional.
SUPPLIER_SEARCH_STATUSES = (0, 1, 2, 3, 5, 7, 8, 9, 10, 11)


def supplier_search_statuses() -> tuple[int, ...]:
	if load_archived_sales_efactura():
		return (*SUPPLIER_SEARCH_STATUSES, SFS_ARCHIVED)
	return SUPPLIER_SEARCH_STATUSES


def sync_supplier_invoices(lookback_days: int | None = None, company: str | None = None) -> dict:
	"""Search supplier invoices and upsert Sales eFactura docs that are missing locally.

	Submitted invoices left in Pending Registration with series and number are refreshed
	from the search result, or via CheckInvoicesStatus when SearchInvoices omits them.
	"""
	lookback_days = int(lookback_days or DEFAULT_LOOKBACK_DAYS)
	totals = {
		"found": 0,
		"created": 0,
		"updated": 0,
		"skipped": 0,
		"details_loaded": 0,
		"errors": 0,
	}
	by_company = []
	for target in get_sync_targets(company):
		client = EFacturaAPIClient.from_settings(
			company=target["company"], timeout=SEARCH_READ_TIMEOUT
		)
		result = _sync_supplier_invoices_for_company(client, target["company"], lookback_days)
		by_company.append({"company": target["company"], **result})
		for key in totals:
			totals[key] += result.get(key, 0)
	totals["by_company"] = by_company
	return totals


def _sync_supplier_invoices_for_company(client, company: str, lookback_days: int) -> dict:
	date_from = add_days(now_datetime(), -lookback_days)
	date_to = now_datetime()

	seen: dict[tuple[str, str], int] = {}
	pending_index = _pending_registration_index(company)
	created = updated = skipped = details = errors = 0
	for st in supplier_search_statuses():
		batch: dict[tuple[str, str], int] = {}
		for inv in iter_search_invoices(
			client,
			actor_role=1,
			invoice_status=st,
			date_from=date_from,
			date_to=date_to,
			error_title=f"Sales eFactura SearchInvoices failed status={st} company={company}",
		):
			seria = str(inv.get("Seria") or "").strip()
			number = str(inv.get("Number") or "").strip()
			if not seria or not number:
				continue
			try:
				code = int(inv.get("InvoiceStatus"))
			except (TypeError, ValueError):
				code = st
			batch[(seria, number)] = code
		seen.update(batch)
		batch_result = _upsert_supplier_invoices(client, company, batch, pending_index)
		created += batch_result["created"]
		updated += batch_result["updated"]
		skipped += batch_result["skipped"]
		details += batch_result["details_loaded"]
		errors += batch_result["errors"]
		frappe.db.commit()

	reconciled, reconcile_errors = _reconcile_pending_registration(client, company, seen)
	updated += reconciled
	errors += reconcile_errors
	frappe.db.commit()

	return {
		"found": len(seen),
		"created": created,
		"updated": updated,
		"skipped": skipped,
		"details_loaded": details,
		"errors": errors,
	}


def _upsert_supplier_invoices(
	client,
	company: str,
	invoices: dict[tuple[str, str], int],
	pending_index: dict[tuple[str, str], str] | None = None,
) -> dict:
	"""Write series and number for one SearchInvoices status. Caller commits."""
	created = updated = skipped = details = errors = 0
	skip_cancelled = do_not_create_cancelled_invoices()
	bank = _default_company_bank(company)
	for (seria, number), ef_status in invoices.items():
		try:
			name = frappe.db.exists(
				"Sales eFactura",
				{"company": company, "ef_series": seria, "ef_number": number},
			)
			if not name and pending_index:
				name = _match_indexed(pending_index, seria, number)
			if name:
				doc = frappe.get_doc("Sales eFactura", name)
				if _apply_remote_status(doc, ef_status, seria, number):
					updated += 1
				if _fill_details(client, doc.name):
					details += 1
				continue

			if skip_cancelled and is_canceled_by_supplier(ef_status):
				skipped += 1
				continue

			xml = _fetch_xml(client, seria, number)
			parsed = parse_invoice_xml(xml) if xml else {}
			buyer_idno = (parsed.get("buyer") or {}).get("idno")
			doc_type = "Non-Transfer" if str(parsed.get("creation_motiv") or "") == "5" else "Transfer"
			party_type = "Supplier" if doc_type == "Non-Transfer" else "Customer"
			party = (
				find_supplier_by_idno(buyer_idno)
				if party_type == "Supplier"
				else find_customer_by_idno(buyer_idno)
			)
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
					"ef_status": sef_status_label(ef_status),
					"company_bank_account": bank,
					"customer_party_type": party_type,
					"customer_party": party,
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
				title=f"Sales eFactura upsert failed {seria}{number} company={company}",
				message=frappe.get_traceback(),
			)

	return {
		"created": created,
		"updated": updated,
		"skipped": skipped,
		"details_loaded": details,
		"errors": errors,
	}


def _normalized_number(number: str) -> str | None:
	text = str(number or "").strip()
	if not text.isdigit():
		return None
	return text.lstrip("0") or "0"


def _pending_registration_index(company: str) -> dict[tuple[str, str], str]:
	"""Submitted Pending Registration docs that already have series and number."""
	rows = frappe.get_all(
		"Sales eFactura",
		filters={
			"company": company,
			"docstatus": 1,
			"ef_status": SEF_PENDING_REGISTRATION,
		},
		fields=["name", "ef_series", "ef_number"],
	)
	index: dict[tuple[str, str], str] = {}
	for row in rows:
		seria = str(row.ef_series or "").strip()
		number = str(row.ef_number or "").strip()
		if not seria or not number:
			continue
		index[(seria, number)] = row.name
		norm = _normalized_number(number)
		if norm and norm != number:
			index.setdefault((seria, norm), row.name)
	return index


def _match_indexed(index: dict[tuple[str, str], str], seria: str, number: str) -> str | None:
	seria = str(seria or "").strip()
	number = str(number or "").strip()
	found = index.get((seria, number))
	if found:
		return found
	norm = _normalized_number(number)
	if not norm:
		return None
	for (key_seria, key_number), name in index.items():
		if key_seria != seria:
			continue
		if _normalized_number(key_number) == norm:
			return name
	return None


def _lookup_remote_status(statuses: dict[tuple[str, str], int], seria: str, number: str):
	"""Return (code, remote_seria, remote_number) for an exact or zero-padded match."""
	seria = str(seria or "").strip()
	number = str(number or "").strip()
	if (seria, number) in statuses:
		return statuses[(seria, number)], seria, number
	norm = _normalized_number(number)
	if not norm:
		return None
	for (remote_seria, remote_number), code in statuses.items():
		if str(remote_seria).strip() != seria:
			continue
		if _normalized_number(remote_number) == norm:
			return code, str(remote_seria).strip(), str(remote_number).strip()
	return None


def _align_series_number(doc, seria: str, number: str) -> None:
	seria = str(seria or "").strip()
	number = str(number or "").strip()
	if not seria or not number:
		return
	if str(doc.ef_series or "").strip() == seria and str(doc.ef_number or "").strip() == number:
		return
	other = frappe.db.exists(
		"Sales eFactura",
		{
			"company": doc.company,
			"ef_series": seria,
			"ef_number": number,
			"name": ["!=", doc.name],
			"docstatus": ["<", 2],
		},
	)
	if other:
		return
	if str(doc.ef_series or "").strip() != seria:
		doc.db_set("ef_series", seria, update_modified=False)
		doc.ef_series = seria
	if str(doc.ef_number or "").strip() != number:
		doc.db_set("ef_number", number, update_modified=False)
		doc.ef_number = number


def _apply_remote_status(doc, code: int, seria: str | None = None, number: str | None = None) -> bool:
	label = sef_status_label(code)
	if not label:
		doc.db_set("last_status_check", now_datetime(), update_modified=False)
		return False
	if seria and number:
		_align_series_number(doc, seria, number)
	changed = doc.ef_status != label
	if changed:
		doc.db_set("ef_status", label, update_modified=False)
		doc.ef_status = label
		doc.set_status()
	doc.db_set("last_status_check", now_datetime(), update_modified=False)
	return changed


def _reconcile_pending_registration(
	client, company: str, seen: dict[tuple[str, str], int]
) -> tuple[int, int]:
	"""Refresh submitted Pending Registration invoices that already have series and number.

	SearchInvoices can miss an invoice that PostInvoices already accepted. CheckInvoicesStatus
	uses the local series and number, the same lookup as Update Status.
	"""
	rows = frappe.get_all(
		"Sales eFactura",
		filters={
			"company": company,
			"docstatus": 1,
			"ef_status": SEF_PENDING_REGISTRATION,
		},
		fields=["name", "ef_series", "ef_number"],
	)
	updated = errors = 0
	to_check = []
	for row in rows:
		seria = str(row.ef_series or "").strip()
		number = str(row.ef_number or "").strip()
		if not seria or not number:
			continue
		found = _lookup_remote_status(seen, seria, number)
		if found is None:
			to_check.append(row)
			continue
		try:
			doc = frappe.get_doc("Sales eFactura", row.name)
			if _apply_remote_status(doc, found[0], found[1], found[2]):
				updated += 1
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"Sales eFactura pending status update failed {seria}{number} company={company}",
				message=frappe.get_traceback(),
			)

	if not to_check:
		return updated, errors

	identifiers = [
		{"Seria": str(row.ef_series).strip(), "Number": str(row.ef_number).strip()} for row in to_check
	]
	try:
		statuses, failed_parts = check_invoices_status_resilient(client, identifiers)
	except Exception:
		frappe.log_error(
			title=f"Sales eFactura pending CheckInvoicesStatus failed company={company}",
			message=frappe.get_traceback(),
		)
		return updated, errors + len(to_check)

	failed_keys = {(str(part.get("Seria")), str(part.get("Number"))) for part in failed_parts}
	for row in to_check:
		seria = str(row.ef_series).strip()
		number = str(row.ef_number).strip()
		if (seria, number) in failed_keys:
			errors += 1
			continue
		found = _lookup_remote_status(statuses, seria, number)
		if found is None:
			continue
		try:
			doc = frappe.get_doc("Sales eFactura", row.name)
			if _apply_remote_status(doc, found[0], found[1], found[2]):
				updated += 1
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"Sales eFactura pending status update failed {seria}{number} company={company}",
				message=frappe.get_traceback(),
			)
	return updated, errors


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
	from erpnext_moldova_efactura.tasks.schedule import (
		FETCH_JOB_TIMEOUT,
		FETCH_SUPPLIER_JOB_ID,
		enqueue_job,
	)

	status = enqueue_job(
		"erpnext_moldova_efactura.tasks.supplier_sync.sync_supplier_invoices",
		FETCH_SUPPLIER_JOB_ID,
		FETCH_JOB_TIMEOUT,
		lookback_days=int(lookback_days),
	)
	return {"status": status}
