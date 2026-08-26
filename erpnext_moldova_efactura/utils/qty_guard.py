"""Guard eFactura submit/save against Sales Invoice stock qty overage."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.si_link import sales_invoice_of


def _qty_precision() -> int:
	return cint(frappe.db.get_default("float_precision")) or 3


def _fmt_qty(qty, precision: int) -> str:
	return frappe.format_value(flt(qty, precision), {"fieldtype": "Float"})


# SFS: 2 = Rejected by Customer, 5 = Canceled by Supplier
FAILED_EF_STATUS_CODES = (2, 5)
FAILED_EF_STATUS_LABELS = ("Rejected by Customer", "Canceled by Supplier")


def is_block_enabled() -> bool:
	return bool(cint(frappe.db.get_single_value("eFactura Settings", "block_submit_on_si_qty_overage")))


def exclude_failed_from_qty_quota() -> bool:
	return bool(
		cint(frappe.db.get_single_value("eFactura Settings", "exclude_failed_efactura_from_qty_quota"))
	)


def warn_on_draft_save_enabled() -> bool:
	return bool(
		cint(frappe.db.get_single_value("eFactura Settings", "warn_si_qty_overage_on_draft_save"))
	)


def is_failed_efactura(ef_status=None, status: str | None = None) -> bool:
	from erpnext_moldova_efactura.utils.fiscal_status import sef_status_label

	label = sef_status_label(ef_status) or (status or "")
	if label in FAILED_EF_STATUS_LABELS:
		return True
	try:
		if ef_status is not None and ef_status != "":
			if int(ef_status) in FAILED_EF_STATUS_CODES:
				return True
	except (TypeError, ValueError):
		pass
	return False


def get_quota_efactura_rows(
	sales_invoice: str,
	exclude_name: str | None = None,
	include_drafts: bool = False,
) -> list[dict]:
	"""eFactura rows that occupy SI qty."""
	filters = {
		"docstatus": ["in", [0, 1]] if include_drafts else 1,
		"sales_invoice": sales_invoice,
	}
	if exclude_name:
		filters["name"] = ["!=", exclude_name]

	rows = frappe.get_all(
		"Sales eFactura",
		filters=filters,
		fields=["name", "ef_status", "status", "docstatus"],
	)
	if exclude_failed_from_qty_quota():
		rows = [
			row
			for row in rows
			if cint(row.docstatus) == 0 or not is_failed_efactura(row.ef_status, row.status)
		]
	return rows


def get_quota_efactura_names(
	sales_invoice: str,
	exclude_name: str | None = None,
	include_drafts: bool | None = None,
) -> list[str]:
	"""Submitted eFactura that occupy SI qty. Drafts included when Settings say so."""
	if include_drafts is None:
		include_drafts = warn_on_draft_save_enabled()
	return [
		row.name
		for row in get_quota_efactura_rows(
			sales_invoice, exclude_name=exclude_name, include_drafts=include_drafts
		)
	]


def find_si_qty_overages(doc, include_drafts: bool = False) -> list[dict]:
	"""
	Return overages vs linked Sales Invoice, aggregated by item_code in stock UOM.

	Each item: this document + other quota eFactura on the same SI
	must not exceed SI stock_qty. Items absent from the SI are treated as SI qty 0.
	"""
	si_name = sales_invoice_of(doc)
	if not si_name:
		return []
	if not frappe.db.exists("Sales Invoice", si_name):
		return []

	precision = _qty_precision()
	current_qty: dict[str, float] = {}
	item_name: dict[str, str] = {}
	stock_uom: dict[str, str] = {}

	for row in doc.get("items") or []:
		code = (row.item_code or "").strip()
		if not code:
			continue
		current_qty[code] = current_qty.get(code, 0) + flt(row.stock_qty)
		if row.item_name:
			item_name[code] = row.item_name
		if row.stock_uom:
			stock_uom[code] = row.stock_uom

	if not current_qty:
		return []

	si_qty: dict[str, float] = {}
	for row in frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": si_name},
		fields=["item_code", "item_name", "stock_qty", "stock_uom"],
	):
		code = (row.item_code or "").strip()
		if not code:
			continue
		si_qty[code] = si_qty.get(code, 0) + flt(row.stock_qty)
		item_name.setdefault(code, row.item_name or code)
		if row.stock_uom:
			stock_uom.setdefault(code, row.stock_uom)

	exclude_name = doc.name if doc.name and not doc.is_new() else None
	other_rows = get_quota_efactura_rows(
		si_name, exclude_name=exclude_name, include_drafts=include_drafts
	)
	other_meta = {row.name: row for row in other_rows}
	other_names = list(other_meta)

	other_qty: dict[str, float] = defaultdict(float)
	other_by_item: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
	if other_names:
		for row in frappe.get_all(
			"Sales eFactura Item",
			filters={
				"parent": ["in", other_names],
				"item_code": ["in", list(current_qty.keys())],
			},
			fields=["parent", "item_code", "stock_qty"],
		):
			code = (row.item_code or "").strip()
			if not code:
				continue
			qty = flt(row.stock_qty)
			other_qty[code] += qty
			other_by_item[code][row.parent] += qty

	overages = []
	for code, this_qty in current_qty.items():
		invoice_qty = flt(si_qty.get(code, 0), precision)
		others = flt(other_qty.get(code, 0), precision)
		this_qty = flt(this_qty, precision)
		total_ef = flt(this_qty + others, precision)
		if total_ef <= invoice_qty:
			continue

		other_docs = []
		for name, qty in sorted(other_by_item.get(code, {}).items()):
			meta = other_meta.get(name) or {}
			other_docs.append(
				{
					"name": name,
					"qty": flt(qty, precision),
					"docstatus": cint(meta.get("docstatus")),
				}
			)
		overages.append(
			{
				"item_code": code,
				"item_name": item_name.get(code) or code,
				"stock_uom": stock_uom.get(code) or "",
				"si_qty": invoice_qty,
				"this_qty": this_qty,
				"other_qty": others,
				"ef_qty": total_ef,
				"other_efacturas": other_docs,
			}
		)

	overages.sort(key=lambda row: row["item_code"])
	return overages


def format_overage_html(
	overages: list[dict],
	sales_invoice: str | None = None,
	include_drafts: bool = False,
) -> str:
	if not overages:
		return ""

	precision = _qty_precision()
	si_link = get_link_to_form("Sales Invoice", sales_invoice) if sales_invoice else ""
	intro = _(
		"Cumulative eFactura quantity exceeds Sales Invoice {0} for the following items:"
	).format(si_link)
	if include_drafts:
		intro += " " + _(
			"Other Draft eFactura are included. The first document submitted takes the quantity; the other will be blocked or need confirmation."
		)

	other_header = _("Other eFactura") if include_drafts else _("Other submitted eFactura")
	headers = [
		_("Item"),
		_("Sales Invoice Qty"),
		_("This eFactura"),
		_("eFactura Qty (total)"),
		other_header,
	]
	head = "".join(f"<th>{escape_html(h)}</th>" for h in headers)
	rows = []
	for row in overages:
		uom = escape_html(row.get("stock_uom") or "")
		label = f"{escape_html(row['item_code'])} — {escape_html(row['item_name'])}"
		others = row.get("other_efacturas") or []
		if others:
			links = ", ".join(_format_other_link(item, uom, precision) for item in others)
		else:
			links = escape_html(_("None"))

		rows.append(
			"<tr>"
			f"<td>{label}</td>"
			f"<td>{_fmt_qty(row['si_qty'], precision)} {uom}</td>"
			f"<td>{_fmt_qty(row['this_qty'], precision)} {uom}</td>"
			f"<td>{_fmt_qty(row['ef_qty'], precision)} {uom}</td>"
			f"<td>{links}</td>"
			"</tr>"
		)

	return (
		f"<p>{intro}</p>"
		"<table class='table table-bordered'>"
		f"<thead><tr>{head}</tr></thead>"
		f"<tbody>{''.join(rows)}</tbody>"
		"</table>"
	)


def _format_other_link(item: dict, uom: str, precision: int) -> str:
	link = get_link_to_form("Sales eFactura", item["name"])
	suffix = f" ({escape_html(_('Draft'))})" if cint(item.get("docstatus")) == 0 else ""
	return f"{link}{suffix} ({_fmt_qty(item['qty'], precision)} {uom})".strip()


def _reset_overage_flag(doc) -> None:
	if hasattr(doc, "si_qty_overage_confirmed"):
		doc.si_qty_overage_confirmed = 0


def enforce_si_qty_on_submit(doc) -> None:
	overages = find_si_qty_overages(doc, include_drafts=False)
	if not overages:
		_reset_overage_flag(doc)
		return

	message = format_overage_html(overages, sales_invoice_of(doc), include_drafts=False)
	block = is_block_enabled()
	confirmed = cint(doc.get("si_qty_overage_confirmed"))

	if block or not confirmed:
		title = (
			_("Quantity exceeds Sales Invoice")
			if block
			else _("Quantity exceeds Sales Invoice — confirmation required")
		)
		frappe.throw(message, title=title)

	_reset_overage_flag(doc)


def enforce_si_qty_on_draft_save(doc) -> None:
	if cint(getattr(doc, "docstatus", 0)) != 0:
		return
	if getattr(doc, "_action", None) == "submit":
		return
	if not warn_on_draft_save_enabled():
		return

	overages = find_si_qty_overages(doc, include_drafts=True)
	if not overages:
		return

	if not cint(doc.get("si_qty_overage_confirmed")):
		frappe.throw(
			format_overage_html(overages, sales_invoice_of(doc), include_drafts=True),
			title=_("Quantity exceeds Sales Invoice — confirmation required"),
		)

	_reset_overage_flag(doc)


@frappe.whitelist()
def check_si_qty_overage(doc, include_drafts=None):
	if isinstance(doc, str):
		doc = frappe.parse_json(doc)
	include_drafts = bool(cint(include_drafts))
	efactura = frappe.get_doc(doc)

	if include_drafts and not warn_on_draft_save_enabled():
		return {
			"block": 0,
			"overages": [],
			"message": "",
			"mode": "save",
		}

	overages = find_si_qty_overages(efactura, include_drafts=include_drafts)
	return {
		"block": 0 if include_drafts else int(is_block_enabled()),
		"overages": overages,
		"message": format_overage_html(
			overages, sales_invoice_of(efactura), include_drafts=include_drafts
		),
		"mode": "save" if include_drafts else "submit",
	}
