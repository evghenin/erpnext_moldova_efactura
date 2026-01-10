import frappe
from frappe import _


@frappe.whitelist()
def actualize_sales_invoice_fiscal_status(sales_invoice):
    """
    Actualize fiscal_status for a single Sales Invoice.
    Called explicitly from UI.
    """
    si = frappe.get_doc("Sales Invoice", sales_invoice)

    if si.docstatus != 1:
        frappe.throw(_("Fiscal status can be actualized only for submitted invoices."))

    from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status

    try:
        new_status = determine_fiscal_status(si)
    except frappe.ValidationError as e:
        # configuration error → show to user
        frappe.throw(str(e))

    si.db_set("fiscal_status", new_status, update_modified=False)

    return {
        "status": new_status,
        "message": _("Fiscal status updated to {0}.").format(new_status or _("empty")),
    }


@frappe.whitelist()
def start_bulk_si_job(names):
    if isinstance(names, str):
        names = frappe.parse_json(names)

    frappe.enqueue(
        method="erpnext_moldova_efactura.api.fiscal_status._bulk_si_job",
        queue="long",
        job_name="Bulk Sales Invoice Fiscal Status Actualization",
        names=names,
        user=frappe.session.user,
    )

    return {"started": True}


def _bulk_si_job(names, user):
    from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status

    total = len(names)
    updated = 0

    for idx, name in enumerate(names, start=1):
        try:
            si = frappe.get_doc("Sales Invoice", name)

            if si.docstatus != 1:
                continue

            new_status = determine_fiscal_status(si)

            if new_status and si.fiscal_status != new_status:
                si.db_set("fiscal_status", new_status, update_modified=False)
                updated += 1

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Bulk fiscal status failed for {name}",
            )

        # publish progress
        frappe.publish_realtime(
            event="bulk_si_fiscal_status_progress",
            message={
                "current": idx,
                "total": total,
            },
            user=user,
        )

    # job done
    frappe.publish_realtime(
        event="bulk_si_fiscal_status_done",
        message={
            "total": total,
            "updated": updated,
        },
        user=user,
    )