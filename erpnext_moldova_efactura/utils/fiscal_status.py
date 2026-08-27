from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_moldova_efactura.utils.buyer_status import status_label

SEF_EF_STATUS_LABELS = {
    -1: "Pending Registration",
    0: "Registered as Draft",
    1: "Signed by Supplier",
    2: "Rejected by Customer",
    3: "Accepted by Customer",
    5: "Canceled by Supplier",
    6: "Archived",
    7: "Sent to Customer",
    8: "Signed by Customer",
    9: "Sent to Customer",
    10: "Transportation",
    11: "Cancellation Requested",
}

SEF_PENDING_REGISTRATION = "Pending Registration"
SEF_REGISTERED_AS_DRAFT = "Registered as Draft"
SEF_CANCELED_BY_SUPPLIER = "Canceled by Supplier"
DOC_STATUSES = ("Draft", "Submitted", "Cancelled", "Return")

# Supplier can cancel / request cancellation in SFS (PostCanceledInvoices).
# Drafts (status 0) are deleted only in the SFS web UI — the SOAP API has no Delete.
SEF_CANCELABLE_LABELS = (
    "Signed by Supplier",
    "Sent to Customer",
    "Accepted by Customer",
    "Signed by Customer",
    "Rejected by Customer",
    "Transportation",
)
SEF_CANCELABLE_STATUSES = (1, 2, 3, 7, 8, 9, 10)


def sfs_status_int(value):
    """Parse an SFS InvoiceStatus code; None if value is already a label."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def sef_status_label(value) -> str:
    """Map SFS InvoiceStatus int (or leftover numeric string) to stored text."""
    code = sfs_status_int(value)
    if code is not None:
        return SEF_EF_STATUS_LABELS.get(code, "")
    return (str(value).strip() if value is not None else "")


def is_sef_pending(value) -> bool:
    return sef_status_label(value) == SEF_PENDING_REGISTRATION


def is_sef_cancelable_status(value) -> bool:
    """True if the supplier can still PostCanceledInvoices for this SFS status."""
    if sef_status_label(value) in SEF_CANCELABLE_LABELS:
        return True
    code = sfs_status_int(value)
    return code in SEF_CANCELABLE_STATUSES if code is not None else False


def sef_workflow_status(ef) -> str:
    """SFS workflow label stored on Sales eFactura.ef_status."""
    label = sef_status_label(getattr(ef, "ef_status", None))
    if label and label not in DOC_STATUSES:
        return label
    legacy = (getattr(ef, "efactura_status", None) or "").strip()
    if legacy and legacy not in DOC_STATUSES:
        return sef_status_label(legacy) or legacy
    status = (getattr(ef, "status", None) or "").strip()
    if status and status not in DOC_STATUSES:
        return status
    return label or status


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

    labels = [sef_workflow_status(ef) for ef in ef_docs]

    # 6) Failed has highest priority
    if any(label in ("Rejected by Customer", "Canceled by Supplier") for label in labels):
        return "Failed"

    # 7) Pending
    if any(label == "Pending Registration" for label in labels):
        return "Pending"

    # 8) In Progress
    if any(
        label
        in (
            "Registered as Draft",
            "Signed by Supplier",
            "Accepted by Customer",
            "Sent to Customer",
            "Pending Registration",
            "Transportation",
        )
        for label in labels
    ):
        return "In Progress"

    # 9) Compare totals
    ef_total = float(
        sum(
            (ef.total or 0)
            for ef, label in zip(ef_docs, labels)
            if label in ("Signed by Customer", "Archived")
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
        fields=["name", "status", "total", "ef_status"],
    )


PI_FISCAL_COMPLETED = ("Signed by Buyer",)
PI_FISCAL_IN_PROGRESS = ("Sent to Buyer", "Accepted", "Transportation")


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


def _buyer_status_fields(name: str, buyer_override=None):
	if buyer_override and getattr(buyer_override, "name", None) == name:
		return frappe._dict(
			ef_status=status_label(getattr(buyer_override, "ef_status", None))
			or getattr(buyer_override, "ef_status", None),
			docstatus=getattr(buyer_override, "docstatus", None),
		)
	row = frappe.db.get_value(
		"Purchase eFactura",
		name,
		["ef_status", "docstatus"],
		as_dict=True,
	)
	if row:
		row.ef_status = status_label(row.ef_status) or row.ef_status
	return row


def _pi_fiscal_cover(pi, buyer_override=None) -> tuple[bool, float, float, float, bool]:
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
	status_by_buyer: dict[str, str | None] = {}
	live_parents: set[str] = set()
	has_draft = False
	for name in parents:
		buyer = _buyer_status_fields(name, buyer_override)
		if not buyer or cint(buyer.docstatus) == 2:
			continue
		live_parents.add(name)
		if cint(buyer.docstatus) == 0:
			has_draft = True
		status_by_buyer[name] = status_label(buyer.ef_status) or buyer.ef_status or None

	signed = 0.0
	in_progress = 0.0
	for row in rows:
		if row.parent not in live_parents:
			continue
		code = status_by_buyer.get(row.parent)
		qty = flt(row.qty) if flt(row.qty) else flt(row.ef_qty)
		if code in PI_FISCAL_COMPLETED:
			signed += qty
		elif code in PI_FISCAL_IN_PROGRESS:
			in_progress += qty
	return bool(live_parents), total, signed, in_progress, has_draft


def apply_draft_suffix(status: str, has_draft: bool) -> str:
	if status and has_draft:
		return f"{status} (Draft)"
	return status


def determine_pi_fiscal_status(pi, buyer_override=None) -> str | None:
	"""Fiscalization label for a submitted Purchase Invoice. Drafts have no status."""
	if cint(getattr(pi, "docstatus", 0)) != 1:
		return None
	has_factura, total, signed, in_progress, has_draft = _pi_fiscal_cover(pi, buyer_override)
	status = classify_pi_fiscal_status(
		individual=_pi_supplier_is_individual(pi),
		has_factura=has_factura,
		total=total,
		signed=signed,
		in_progress=in_progress,
	)
	return apply_draft_suffix(status, has_draft)


def sync_pi_fiscal_status(pi_name, pi=None, buyer_override=None):
    if not pi_name:
        return None
    if not frappe.db.exists("Purchase Invoice", pi_name):
        return None
    if not frappe.get_meta("Purchase Invoice").has_field("fiscal_status"):
        return None
    pi = pi or frappe.get_doc("Purchase Invoice", pi_name)
    status = determine_pi_fiscal_status(pi, buyer_override=buyer_override) or ""
    if (pi.get("fiscal_status") or "") != status:
        frappe.db.set_value("Purchase Invoice", pi.name, "fiscal_status", status, update_modified=False)
        pi.fiscal_status = status
    return status or None


SEF_FISCAL_COMPLETED = ("Signed by Customer", "Archived")
SEF_FISCAL_IN_PROGRESS = (
	"Registered as Draft",
	"Signed by Supplier",
	"Accepted by Customer",
	"Sent to Customer",
	"Pending Registration",
	"Transportation",
)
SEF_FISCAL_FAILED = ("Rejected by Customer", "Canceled by Supplier")


def _pr_supplier_is_individual(pr) -> bool:
	supplier = getattr(pr, "supplier", None)
	if not supplier:
		return False
	return (frappe.db.get_value("Supplier", supplier, "supplier_type") or "") == "Individual"


def _sef_cover_for_pr(pr) -> tuple[bool, float, float, float, bool, bool]:
	"""has_factura, abs_pr_qty, signed_qty, in_progress_qty, has_draft, has_failed."""
	from erpnext_moldova_efactura.utils.sef_pr_alloc import get_sef_names_for_pr

	items = getattr(pr, "items", None) or []
	total = sum(abs(flt(row.qty)) for row in items)
	pr_name = getattr(pr, "name", None)
	if not pr_name or not frappe.db.has_column("Sales eFactura Item", "purchase_receipt"):
		return False, total, 0.0, 0.0, False, False

	rows = frappe.get_all(
		"Sales eFactura Item",
		filters={"purchase_receipt": pr_name, "parenttype": "Sales eFactura"},
		fields=["parent", "qty", "ef_qty"],
	)
	parents = list({row.parent for row in rows if row.parent} | set(get_sef_names_for_pr(pr_name)))
	if getattr(pr, "sales_efactura", None):
		parents.append(pr.sales_efactura)
	parents = list({name for name in parents if name})
	if not parents:
		return False, total, 0.0, 0.0, False, False

	signed = 0.0
	in_progress = 0.0
	has_draft = False
	has_failed = False
	live = set()
	status_by = {}
	for name in parents:
		row = frappe.db.get_value("Sales eFactura", name, ["ef_status", "docstatus"], as_dict=True)
		if not row or cint(row.docstatus) == 2:
			continue
		live.add(name)
		label = sef_status_label(row.ef_status) or row.ef_status
		status_by[name] = label
		if cint(row.docstatus) == 0:
			has_draft = True
		if label in SEF_FISCAL_FAILED:
			has_failed = True
	for row in rows:
		if row.parent not in live:
			continue
		code = status_by.get(row.parent)
		qty = abs(flt(row.qty) if flt(row.qty) else flt(row.ef_qty))
		if code in SEF_FISCAL_COMPLETED:
			signed += qty
		elif code in SEF_FISCAL_IN_PROGRESS:
			in_progress += qty
	return bool(live), total, signed, in_progress, has_draft, has_failed


def determine_return_pr_fiscal_status(pr) -> str | None:
	if cint(getattr(pr, "docstatus", 0)) != 1:
		return None
	if _pr_supplier_is_individual(pr):
		return "Not Required"
	has_factura, total, signed, in_progress, has_draft, has_failed = _sef_cover_for_pr(pr)
	if has_failed:
		status = "Failed"
	else:
		status = classify_pi_fiscal_status(
			individual=False,
			has_factura=has_factura,
			total=total,
			signed=signed,
			in_progress=in_progress,
		)
	return apply_draft_suffix(status, has_draft)


def sales_invoices_for_purchase_receipt(pr) -> list[str]:
	"""SIs that fiscalize a regular (non-return) Purchase Receipt."""
	pr_name = getattr(pr, "name", None) or pr
	if not pr_name:
		return []
	items = getattr(pr, "items", None)
	if items is None:
		items = frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": pr_name},
			fields=["sales_order", "purchase_order"],
		)
	so_names = list({(row.sales_order or "").strip() for row in items if getattr(row, "sales_order", None)})
	po_names = list({(row.purchase_order or "").strip() for row in items if getattr(row, "purchase_order", None)})
	found: list[str] = []
	seen: set[str] = set()

	def _collect(field, names):
		if not names:
			return
		for name in frappe.get_all(
			"Sales Invoice Item",
			filters={field: ["in", names]},
			pluck="parent",
		):
			if not name or name in seen:
				continue
			if cint(frappe.db.get_value("Sales Invoice", name, "docstatus")) != 1:
				continue
			seen.add(name)
			found.append(name)

	_collect("sales_order", so_names)
	_collect("purchase_order", po_names)
	return found


def purchase_receipts_for_sales_invoice(si_name: str) -> list[str]:
	if not si_name:
		return []
	so_names = [
		row
		for row in frappe.get_all(
			"Sales Invoice Item", filters={"parent": si_name}, pluck="sales_order"
		)
		if row
	]
	po_names = [
		row
		for row in frappe.get_all(
			"Sales Invoice Item", filters={"parent": si_name}, pluck="purchase_order"
		)
		if row
	]
	found: list[str] = []
	seen: set[str] = set()

	def _collect(field, names):
		if not names:
			return
		for name in frappe.get_all(
			"Purchase Receipt Item",
			filters={"parenttype": "Purchase Receipt", field: ["in", names]},
			pluck="parent",
		):
			if name and name not in seen:
				seen.add(name)
				found.append(name)

	_collect("sales_order", so_names)
	_collect("purchase_order", po_names)
	out = []
	for name in found:
		if cint(frappe.db.get_value("Purchase Receipt", name, "is_return")):
			continue
		if cint(frappe.db.get_value("Purchase Receipt", name, "docstatus")) != 1:
			continue
		out.append(name)
	return out


def _si_stored_fiscal_status(si_name: str) -> str:
	label = (frappe.db.get_value("Sales Invoice", si_name, "fiscal_status") or "").strip()
	if label:
		return label
	si = frappe.get_doc("Sales Invoice", si_name)
	return determine_fiscal_status(si) or ""


def _worst_mirrored_fiscal(labels: list[str]) -> str:
	nonempty = [label for label in labels if label]
	if not nonempty:
		return "Pending"
	if len(set(nonempty)) == 1:
		return nonempty[0]
	bases = [(label or "").replace(" (Draft)", "").strip() for label in nonempty]
	priority = ("Failed", "Partial", "Pending", "In Progress", "Completed", "Not Applicable", "Not Required")
	for status in priority:
		if status in bases:
			return status
	return "Unknown"


def determine_pr_fiscal_status(pr) -> str | None:
	if cint(getattr(pr, "docstatus", 0)) != 1:
		return None
	if cint(getattr(pr, "is_return", 0)):
		return determine_return_pr_fiscal_status(pr)
	si_names = sales_invoices_for_purchase_receipt(pr)
	if not si_names:
		return "Pending"
	return _worst_mirrored_fiscal([_si_stored_fiscal_status(name) for name in si_names])


def sync_pr_fiscal_status(pr_name, pr=None) -> str | None:
	if not pr_name:
		return None
	if not frappe.db.exists("Purchase Receipt", pr_name):
		return None
	if not frappe.get_meta("Purchase Receipt").has_field("fiscal_status"):
		return None
	pr = pr or frappe.get_doc("Purchase Receipt", pr_name)
	status = determine_pr_fiscal_status(pr) or ""
	if (pr.get("fiscal_status") or "") != status:
		frappe.db.set_value("Purchase Receipt", pr.name, "fiscal_status", status, update_modified=False)
		pr.fiscal_status = status
	return status or None


def sync_prs_for_sales_invoice(si_name: str) -> None:
	for pr_name in purchase_receipts_for_sales_invoice(si_name):
		sync_pr_fiscal_status(pr_name)
