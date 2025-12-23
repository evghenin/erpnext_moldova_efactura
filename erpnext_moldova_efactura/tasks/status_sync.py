import frappe
from frappe.utils import now_datetime
from erpnext_moldova_efactura.api_client import EFacturaAPIClient


CHECKABLE_EF_STATUSES = (
    0,  # Draft
    1,  # Signed by Supplier
    # 2,  # Rejected by Customer
    3,  # Accepted by Customer
    # 5,  # Canceled by Supplier
    7,  # Sent to Customer
    # 8,  # Signed by Customer
    # 10, # Transported
)

BATCH_SIZE = 50

def sync_efactura_statuses():
    started_at = now_datetime()

    docs = frappe.db.sql(
        """
        SELECT
            name,
            ef_series,
            ef_number,
            ef_status,
            last_status_check
        FROM `tabeFactura`
        WHERE
            docstatus = 1
            AND ef_status IN %(statuses)s
            AND ef_series IS NOT NULL AND ef_series != ''
            AND ef_number IS NOT NULL AND ef_number != ''
        ORDER BY
            CASE
                WHEN last_status_check IS NULL THEN 0
                ELSE 1
            END,
            last_status_check ASC
        LIMIT %(limit)s
        """,
        {"statuses": CHECKABLE_EF_STATUSES, "limit": BATCH_SIZE},
        as_dict=True,
    )

    if not docs:
        return

    seria_and_numbers = [{"Seria": row.ef_series, "Number": row.ef_number} for row in docs]

    client = EFacturaAPIClient.from_settings()

    try:
        response = client.check_invoices_status(seria_and_numbers=seria_and_numbers)
    except Exception:
        frappe.log_error(
            title="eFactura batch status request failed",
            message=frappe.get_traceback(),
        )
        return

    statuses = _extract_status_map(response)
    now_ts = now_datetime()

    total = len(docs)
    updated = 0
    unchanged = 0
    missing_count = 0
    errors = 0

    missing_docs = []

    for row in docs:
        try:
            key = (str(row.ef_series), str(row.ef_number))
            new_status = statuses.get(key)

            if new_status is None:
                missing_count += 1
                if len(missing_docs) < 5:
                    missing_docs.append(f"{row.ef_series}{row.ef_number}")
                continue

            doc = frappe.get_doc("eFactura", row.name)

            if doc.ef_status != new_status:
                doc.db_set("ef_status", new_status, update_modified=False)
                doc.set_status()
                updated += 1
            else:
                unchanged += 1

            doc.db_set("last_status_check", now_ts, update_modified=False)

        except Exception:
            errors += 1

    if missing_count or errors:
        msg_lines = [
            f"Started at: {started_at}",
            f"Batch size: {total}",
            f"Updated: {updated}",
            f"Unchanged: {unchanged}",
            f"Missing in API response: {missing_count}",
            f"Errors: {errors}",
        ]
        if missing_docs:
            msg_lines.append(f"Missing documents: {', '.join(missing_docs)}")

        # Логируем один раз по итогу процедуры
        frappe.log_error(
            title="eFactura status sync summary (with issues)",
            message="\n".join(msg_lines),
        )


def _extract_status_map(response: dict) -> dict:
    """
    Returns {(Seria, Number): int_status_code}
    """
    result = {}

    items = (
        response.get("Results", {})
        .get("Invoice", [])
    )

    if isinstance(items, dict):
        items = [items]

    for item in items:
        seria = item.get("Seria")
        number = item.get("Number")
        raw_status = item.get("InvoiceStatus")

        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            continue

        if seria and number:
            result[(str(seria), str(number))] = status_code

    return result