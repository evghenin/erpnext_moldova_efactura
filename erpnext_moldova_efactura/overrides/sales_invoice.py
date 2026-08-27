import frappe
from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status


def before_insert(doc, method=None):
    if doc.meta.has_field("sales_efactura") and doc.get("sales_efactura"):
        return
    so_name = next((row.sales_order for row in (doc.items or []) if row.sales_order), None)
    if not so_name or not frappe.get_meta("Sales Order").has_field("sales_efactura"):
        return
    sef = frappe.db.get_value("Sales Order", so_name, "sales_efactura")
    if sef and doc.meta.has_field("sales_efactura"):
        doc.sales_efactura = sef


def after_insert(doc, method=None):
    from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
        link_created_sales_invoice,
    )

    link_created_sales_invoice(doc)


def on_submit(doc, method=None):
    """
    Set fiscal_status on Sales Invoice submit
    """
    status = determine_fiscal_status(doc)

    doc.db_set("fiscal_status", status, update_modified=False)
    from erpnext_moldova_efactura.utils.fiscal_status import sync_prs_for_sales_invoice

    sync_prs_for_sales_invoice(doc.name)


def on_cancel(doc, method=None):
    from erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura import (
        unlink_created_sales_invoice,
    )
    from erpnext_moldova_efactura.utils.fiscal_status import sync_prs_for_sales_invoice

    unlink_created_sales_invoice(doc)
    sync_prs_for_sales_invoice(doc.name)
