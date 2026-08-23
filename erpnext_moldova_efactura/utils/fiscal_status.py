from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt


def determine_fiscal_status(si):
    # Draft documents are ignored
    if si.docstatus != 1:
        return None

    customer = frappe.get_doc("Customer", si.customer)

    # 1) Not Company
    if customer.customer_type != "Company":
        return "Not Required"

    # 2) Ensure configuration
    ensure_fiscal_territory_configured(si)

    # 3) Out of fiscal scope
    if not territory_in_fiscal_scope(customer.territory):
        return "Not Applicable"

    # 4) Load e-Factura documents
    ef_docs = get_efacturas_for_invoice(si.name)

    # 5) No e-Factura yet
    if not ef_docs:
        return "Pending"

    # 6) Failed has highest priority
    for ef in ef_docs:
        if ef.status in (
            "Rejected by Customer", 
            "Canceled by Supplier", 
            ):
            return "Failed"

    # 7) Pending
    for ef in ef_docs:
        if ef.status in (
            "Pending Registration", 
            ):
            return "Pending"
        
    # 8) In Progress
    for ef in ef_docs:
        if ef.status in (
            "Registered as Draft", 
            "Signed by Supplier", 
            "Accepted by Customer",
            "Sent to Customer", 
            "Pending Registration", 
            "Transportation"
            ):

            return "In Progress"

    # 9) Compare totals
    ef_total = float(
        sum(
            (ef.total or 0)
            for ef in ef_docs
            if ef.status in ("Signed by Customer", "Archived")
        )
    )
    si_total = float(si.grand_total or 0)

    if ef_total < si_total:
        return "Partial"

    if ef_total == si_total:
        return "Completed"

    # 10) Any unexpected situation
    return "Unknown"


def territory_in_fiscal_scope(customer_territory: str) -> bool:
    """
    Returns True if customer territory is within fiscal territory
    defined in eFactura Settings (including nested territories).
    """
    if not customer_territory:
        return False

    settings = frappe.get_single("eFactura Settings")
    fiscal_root = settings.get("fiscal_territory")

    if not fiscal_root:
        return False

    # get lft, rgt of fiscal root
    fiscal = frappe.get_value(
        "Territory",
        fiscal_root,
        ["lft", "rgt"],
        as_dict=True,
    )

    if not fiscal:
        return False

    # get lft, rgt of customer territory
    customer = frappe.get_value(
        "Territory",
        customer_territory,
        ["lft", "rgt"],
        as_dict=True,
    )

    if not customer:
        return False

    # nested set check
    return (
        customer.lft >= fiscal.lft
        and customer.rgt <= fiscal.rgt
    )

def ensure_fiscal_territory_configured(doc=None):
    settings = frappe.get_single("eFactura Settings")

    if settings.get("fiscal_territory"):
        return

    message = _(
        "Sales Invoice could not be submitted because eFactura is not configured. "
        "Please set Fiscal Territory in eFactura Settings."
    )

    # Add comment to document
    doc.add_comment("Comment", message)

    frappe.throw(
        message,
        title=_("eFactura Configuration Required. Fiscal Territory must be set."),
    )

def get_efacturas_for_invoice(si_name):
    """
    Returns all non-cancelled eFactura linked to Sales Invoice
    """
    return frappe.get_all(
        "Sales eFactura",
        filters={
            "sales_invoice": si_name,
            "docstatus": ["!=", 2],
        },
        fields=["name", "status", "total"],
    )


PI_FISCAL_COMPLETED = (8,)
PI_FISCAL_IN_PROGRESS = (7, 9, 3, 10)


def classify_pi_fiscal_status(
	*,
	individual: bool,
	has_factura: bool,
	total: float,
	signed: float,
	in_progress: float,
	precision: int | None = None,
) -> str:
	"""Map PI coverage + supplier type onto a fiscalization label."""
	if individual:
		return "Not Required"
	if not has_factura:
		return "Pending"
	if precision is None:
		from erpnext_moldova_efactura.utils.pi_match import qty_precision

		precision = qty_precision()
	need = flt(total, precision)
	if need <= 0:
		return "Pending"
	done = flt(signed, precision)
	if done >= need:
		return "Completed"
	if done > 0:
		return "Partial"
	if flt(in_progress, precision) >= need:
		return "In Progress"
	return "Pending"


def _pi_supplier_is_individual(pi) -> bool:
	supplier = getattr(pi, "supplier", None)
	if not supplier:
		return False
	supplier_type = frappe.db.get_value("Supplier", supplier, "supplier_type")
	return (supplier_type or "") == "Individual"


def _pi_fiscal_cover(pi) -> tuple[bool, float, float, float, bool]:
	"""(has_factura, total_qty, signed_qty, in_progress_qty, has_draft_factura)."""
	items = getattr(pi, "items", None) or []
	total = sum(flt(row.qty) for row in items)
	pi_name = getattr(pi, "name", None)
	if not pi_name or not frappe.db.has_column("Purchase eFactura Item", "purchase_invoice"):
		return False, total, 0.0, 0.0, False

	rows = frappe.get_all(
		"Purchase eFactura Item",
		filters={"purchase_invoice": pi_name, "parenttype": "Purchase eFactura"},
		fields=["parent", "qty", "ef_qty", "name"],
	)
	if not rows:
		return False, total, 0.0, 0.0, False

	parents = list({row.parent for row in rows if row.parent})
	status_by_buyer: dict[str, int | None] = {}
	has_draft = False
	for name in parents:
		buyer = frappe.db.get_value(
			"Purchase eFactura",
			name,
			["ef_status", "docstatus"],
			as_dict=True,
		)
		if not buyer or cint(buyer.docstatus) == 2:
			status_by_buyer[name] = None
			continue
		if cint(buyer.docstatus) == 0:
			has_draft = True
		try:
			status_by_buyer[name] = int(buyer.ef_status)
		except (TypeError, ValueError):
			status_by_buyer[name] = None

	signed = 0.0
	in_progress = 0.0
	has_factura = False
	for row in rows:
		code = status_by_buyer.get(row.parent)
		if code is None:
			continue
		has_factura = True
		qty = flt(row.qty) if flt(row.qty) else flt(row.ef_qty)
		if code in PI_FISCAL_COMPLETED:
			signed += qty
		elif code in PI_FISCAL_IN_PROGRESS:
			in_progress += qty
	return has_factura, total, signed, in_progress, has_draft


def apply_draft_suffix(status: str, has_draft: bool) -> str:
	if status and has_draft:
		return f"{status} (Draft)"
	return status


def determine_pi_fiscal_status(pi) -> str | None:
	"""Fiscalization label for a submitted Purchase Invoice. Drafts have no status."""
	if cint(getattr(pi, "docstatus", 0)) != 1:
		return None
	has_factura, total, signed, in_progress, has_draft = _pi_fiscal_cover(pi)
	status = classify_pi_fiscal_status(
		individual=_pi_supplier_is_individual(pi),
		has_factura=has_factura,
		total=total,
		signed=signed,
		in_progress=in_progress,
	)
	return apply_draft_suffix(status, has_draft)


def sync_pi_fiscal_status(pi_name, pi=None):
    if not pi_name:
        return None
    if not frappe.db.exists("Purchase Invoice", pi_name):
        return None
    if not frappe.get_meta("Purchase Invoice").has_field("fiscal_status"):
        return None
    pi = pi or frappe.get_doc("Purchase Invoice", pi_name)
    status = determine_pi_fiscal_status(pi) or ""
    if (pi.get("fiscal_status") or "") != status:
        frappe.db.set_value("Purchase Invoice", pi.name, "fiscal_status", status, update_modified=False)
        pi.fiscal_status = status
    return status or None
