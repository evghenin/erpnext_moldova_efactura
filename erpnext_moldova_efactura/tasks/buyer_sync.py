"""Sync incoming e-Factura invoices (ActorRole=2) into Purchase eFactura."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, now_datetime

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import extract_invoices, invoice_status_map, invoice_xml
from erpnext_moldova_efactura.utils.buyer_status import (
	BUYER_ACTIONABLE_LABELS,
	buyer_search_statuses,
	do_not_create_cancelled_invoices,
	is_buyer_actionable_status,
	is_canceled_by_supplier,
	status_label,
)
from erpnext_moldova_efactura.utils.company_api import get_sync_targets
from erpnext_moldova_efactura.utils.search_windows import iter_search_invoices

DEFAULT_LOOKBACK_DAYS = 180
BATCH_DETAIL = 100


def sync_buyer_invoices(lookback_days: int | None = None, company: str | None = None) -> dict:
	"""Search buyer invoices and upsert Purchase eFactura docs. Read + local write only."""
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
		client = EFacturaAPIClient.from_settings(company=target["company"])
		result = _sync_buyer_invoices_for_company(client, target["company"], lookback_days)
		by_company.append({"company": target["company"], **result})
		for key in totals:
			totals[key] += result.get(key, 0)
	totals["by_company"] = by_company
	return totals


def _sync_buyer_invoices_for_company(client, company: str, lookback_days: int) -> dict:
	date_from = add_days(now_datetime(), -lookback_days)
	date_to = now_datetime()

	seen: dict[tuple[str, str], int] = {}
	for st in buyer_search_statuses():
		for inv in iter_search_invoices(
			client,
			actor_role=2,
			invoice_status=st,
			date_from=date_from,
			date_to=date_to,
			error_title=f"Purchase eFactura SearchInvoices failed status={st} company={company}",
		):
			_remember_invoice(seen, inv, default_status=st)

	# IssuedOn search often misses buyer-inbox invoices still at "Signed by Supplier".
	_merge_signed_by_supplier_inbox(client, seen, company)

	created = updated = skipped = details = errors = 0
	skip_cancelled = do_not_create_cancelled_invoices()
	# Fetch XML details for a limited batch (prefer awaiting-action, then missing items/xml)
	detail_candidates = []

	for (seria, number), ef_status in seen.items():
		try:
			name = frappe.db.exists(
				"Purchase eFactura",
				{"company": company, "ef_series": seria, "ef_number": number},
			)
			priority = 0 if is_buyer_actionable_status(ef_status) else 1
			if name:
				doc = frappe.get_doc("Purchase eFactura", name)
				if cint(doc.docstatus) != 0:
					doc.persist_sfs_status(ef_status)
					updated += 1
					continue
				doc.ef_status = status_label(ef_status)
				doc.last_status_check = now_datetime()
				doc.set_status(update=False)
				doc.save(ignore_permissions=True)
				updated += 1
				# Drafts always re-read XML so totals/items can be corrected
				detail_candidates.append((priority, doc.name))
			else:
				if skip_cancelled and is_canceled_by_supplier(ef_status):
					skipped += 1
					continue
				doc = frappe.get_doc(
					{
						"doctype": "Purchase eFactura",
						"naming_series": "ACC-PEF-.YYYY.-",
						"company": company,
						"ef_series": seria,
						"ef_number": number,
						"ef_status": status_label(ef_status),
						"status": "Draft",
						"last_status_check": now_datetime(),
					}
				)
				doc.flags.from_efactura_sync = True
				doc.insert(ignore_permissions=True)
				created += 1
				detail_candidates.append((priority, doc.name))
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"Purchase eFactura upsert failed {seria}{number} company={company}",
				message=frappe.get_traceback(),
			)

	detail_candidates.sort(key=lambda row: row[0])
	for _priority, name in detail_candidates[:BATCH_DETAIL]:
		try:
			if _fill_details(client, name):
				details += 1
		except Exception:
			errors += 1
			frappe.log_error(
				title=f"Purchase eFactura detail fetch failed {name}",
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


def _remember_invoice(seen: dict[tuple[str, str], int], inv: dict, default_status: int | None = None) -> None:
	seria = str(inv.get("Seria") or "").strip()
	number = str(inv.get("Number") or "").strip()
	if not seria or not number:
		return
	try:
		code = int(inv.get("InvoiceStatus"))
	except (TypeError, ValueError):
		code = default_status if default_status is not None else 1
	seen[(seria, number)] = code


def _merge_signed_by_supplier_inbox(client: EFacturaAPIClient, seen: dict[tuple[str, str], int], company: str) -> None:
	"""Buyer invoices at SFS status 1 are visible in the portal but often absent from IssuedOn SearchInvoices."""
	try:
		resp = client.search_invoices(actor_role=2, parameters={"InvoiceStatus": 1})
	except Exception:
		frappe.log_error(
			title=f"Purchase eFactura SearchInvoices failed status=1 (no date) company={company}",
			message=frappe.get_traceback(),
		)
	else:
		for inv in extract_invoices(resp):
			_remember_invoice(seen, inv, default_status=1)

	for order in (1, 2):
		try:
			resp = client.get_invoices_for_signing(actor_role=2, order=order)
		except Exception:
			frappe.log_error(
				title=f"Purchase eFactura GetInvoicesForSigning failed order={order} company={company}",
				message=frappe.get_traceback(),
			)
			continue
		for inv in extract_invoices(resp):
			_remember_invoice(seen, inv, default_status=1)


def _fill_details(client: EFacturaAPIClient, name: str) -> bool:
	doc = frappe.get_doc("Purchase eFactura", name)
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
		doc.ef_status = status_label(inv.get("InvoiceStatus"))

	xml = invoice_xml(inv)
	if not xml:
		doc.last_status_check = now_datetime()
		doc.set_status(update=False)
		doc.save(ignore_permissions=True)
		return False

	preserve_party = doc.supplier_party
	preserve_party_type = doc.supplier_party_type
	doc.flags.allow_sfs_item_refresh = True
	doc.fill_from_xml(xml, preserve_mapped_items=True)
	# Party type is derived from is_return; keep a user-chosen party of the same type.
	if preserve_party and doc.supplier_party_type == (preserve_party_type or doc.supplier_party_type):
		doc.supplier_party = preserve_party
	elif doc.ef_supplier_idno and not doc.supplier_party:
		from erpnext_moldova_efactura.utils.pef_mode import resolve_xml_supplier_party

		resolve_xml_supplier_party(doc)

	doc.last_status_check = now_datetime()
	doc.set_status(update=False)
	doc.save(ignore_permissions=True)
	return True


def get_buyer_status_sync_rows(company: str, batch_size: int = 50) -> list[dict]:
	"""Oldest last_status_check first, but invoices awaiting buyer action always lead.

	SFS CheckInvoicesStatus is batched. Prefer statuses 1/7/9 so Sign/Accept
	queues stay current even when many accepted/archived docs exist.
	"""
	limit = max(int(batch_size or 50), 1)
	actionable = ", ".join(frappe.db.escape(label) for label in BUYER_ACTIONABLE_LABELS)
	return frappe.db.sql(
		f"""
		SELECT name, ef_series, ef_number, ef_status
		FROM `tabPurchase eFactura`
		WHERE company = %s
			AND ifnull(ef_series, '') != ''
			AND ifnull(ef_number, '') != ''
		ORDER BY
			CASE WHEN ef_status IN ({actionable}) THEN 0 ELSE 1 END ASC,
			ifnull(last_status_check, '1000-01-01') ASC,
			modified ASC
		LIMIT {limit}
		""",
		(company,),
		as_dict=True,
	)


def sync_buyer_statuses(batch_size: int = 50) -> dict:
	"""Refresh ef_status for existing buyer docs via CheckInvoicesStatus."""
	checked = updated = 0
	for target in get_sync_targets():
		rows = get_buyer_status_sync_rows(target["company"], batch_size)
		if not rows:
			continue
		client = EFacturaAPIClient.from_settings(company=target["company"])
		payload = [{"Seria": r.ef_series, "Number": r.ef_number} for r in rows]
		try:
			resp = client.check_invoices_status(seria_and_numbers=payload)
		except Exception:
			frappe.log_error(
				title=f"Purchase eFactura status sync failed company={target['company']}",
				message=frappe.get_traceback(),
			)
			continue

		statuses = invoice_status_map(resp)
		for row in rows:
			key = (str(row.ef_series), str(row.ef_number))
			new_status = statuses.get(key)
			doc = frappe.get_doc("Purchase eFactura", row.name)
			if new_status is not None and doc.ef_status != status_label(new_status):
				updated += 1
			doc.persist_sfs_status(new_status)
		checked += len(rows)

	frappe.db.commit()
	return {"checked": checked, "updated": updated}


@frappe.whitelist()
def fetch_buyer_invoices(lookback_days: int = 180):
	if not frappe.has_permission("Purchase eFactura", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return sync_buyer_invoices(lookback_days=int(lookback_days))
