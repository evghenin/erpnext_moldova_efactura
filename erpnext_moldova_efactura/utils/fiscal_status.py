import frappe
from frappe import _


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
        if ef.status in ("Rejected", "Cancelled", "Error"):
            return "Failed"

    # 7) In Progress
    for ef in ef_docs:
        if ef.status in ("Draft", "Sent", "Pending", "Processing"):
            return "In Progress"

    # 8) Compare totals
    ef_total = sum((ef.total or 0) for ef in ef_docs)
    si_total = si.grand_total or 0

    if ef_total < si_total:
        return "Partial"

    if ef_total == si_total:
        return "Completed"

    # 9) Any unexpected situation
    return "Failed"


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
        "eFactura",
        filters={
            "sales_invoice": si_name,
            "docstatus": ["!=", 2],
        },
        fields=["name", "status", "total"],
    )