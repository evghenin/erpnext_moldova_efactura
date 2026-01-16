import frappe
from frappe.utils import now_datetime, add_days
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
DRAFT = 0
CANCELLED_BY_SUPPLIER = 5
DEFAULT_LOOKBACK_DAYS = 365
MAX_RESULTS_PER_RUN = 20000  # safety limit
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


def sync_efactura_cancelled_from_search_invoices():
    """
    Daily job:
    - Pull invoices from e-Factura via SearchInvoices
    - Filter InvoiceStatus == 5 (Canceled by Supplier)
    - Update local docs to ef_status = 5
    """
    settings = frappe.get_single("eFactura Settings")

    lookback_days = int(getattr(settings, "cancel_sync_lookback_days", None) or DEFAULT_LOOKBACK_DAYS)
    date_from = add_days(now_datetime(), -lookback_days)

    date_to = now_datetime()

    # Build API call
    from erpnext_moldova_efactura.api_client import EFacturaAPIClient
    client = EFacturaAPIClient.from_settings()

    # NOTE: parameter names depend on the SOAP contract you use.
    # Replace keys below to match your API spec if needed.
    parameters = {
        "InvoiceStatus": CANCELLED_BY_SUPPLIER,
        "IssuedOn": {
            "StartDate": date_from
        }
    }

    try:
        resp = client.search_invoices(actor_role=1, parameters=parameters)
    except Exception:
        frappe.log_error(title="e-Factura SearchInvoices failed", message=frappe.get_traceback())
        return

    cancelled = _extract_rows_from_invoices_response(resp)
    
    if not cancelled:
        return

    # Optional: limit to avoid excessive DB load
    cancelled = cancelled[:MAX_RESULTS_PER_RUN]

    updated = _apply_cancelled_status_to_local_docs(cancelled)

    frappe.logger().info(
        f"e-Factura cancelled sync finished. from={date_from} to={date_to} cancelled={len(cancelled)} updated={updated}"
    )


def _extract_rows_from_invoices_response(resp: dict) -> list[tuple[str, str, int]]:
    """
    Returns list of (Seria, Number, Status) for InvoiceStatus == 5
    """
    out: list[tuple[str, str, int]] = []
    if not isinstance(resp, dict):
        return out

    results = resp.get("Results") or resp
    invoices = results.get("Invoice") if isinstance(results, dict) else None

    if not invoices:
        return out

    # Sometimes SOAP serializers return a dict for single item
    if isinstance(invoices, dict):
        invoices = [invoices]

    for inv in invoices:
        if not isinstance(inv, dict):
            continue
        try:
            status = int(inv.get("InvoiceStatus"))
        except Exception:
            continue

        if status != CANCELLED_BY_SUPPLIER:
            continue

        seria = (inv.get("Seria") or "").strip()
        number = (inv.get("Number") or "").strip()
        status = int(inv.get("InvoiceStatus"))

        if seria and number and status:
            out.append((seria, number, status))

    # Deduplicate
    out = list(dict.fromkeys(out))
    return out


def _apply_cancelled_status_to_local_docs(keys: list[tuple[str, str, int]]) -> int:
    """
    Update local records. Adjust Doctype/fields below to match your data model.
    """
    updated = 0
    now_ts = now_datetime()

    for seria, number, status in keys:
        if status != CANCELLED_BY_SUPPLIER:
            continue

        # Example Doctype: "eFactura" (adjust if you store ef fields in Sales Invoice)
        name = frappe.db.get_value(
            "eFactura",
            {"ef_series": seria, "ef_number": number, "docstatus": 1},
            "name",
        )
        if not name:
            continue

        doc = frappe.get_doc("eFactura", name)

        if int(doc.ef_status or 0) != status:
            doc.db_set("ef_status", status, update_modified=False)
            doc.set_status()
            updated += 1

        doc.db_set("last_status_check", now_ts, update_modified=False)

    return updated
