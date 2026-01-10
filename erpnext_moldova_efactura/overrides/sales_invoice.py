import frappe
from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status


def on_submit(doc, method=None):
    """
    Set fiscal_status on Sales Invoice submit
    """
    status = determine_fiscal_status(doc)

    doc.db_set("fiscal_status", status, update_modified=False)
