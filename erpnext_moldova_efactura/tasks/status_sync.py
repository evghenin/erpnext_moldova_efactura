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
    9,  # Sent to Customer
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


def sync_efactura_draft_invoices_by_api_invoice_id():
    """Sync series/number/status for locally Draft invoices using APIInvoiceId.

    Use case: invoices were posted to e-Factura as *unsigned* XML, therefore the local
    document may remain ef_status == 0 (Draft) and without ef_series/ef_number, while
    e-Factura may already have assigned a series/number and an updated status.

    Strategy:
    - Select submitted local docs with ef_status == 0 (Draft)
    - For each doc call SearchInvoices with Parameters.APIInvoiceId == doc.name
    - Expect a single invoice in response; update ef_series, ef_number, ef_status locally
    """

    started_at = now_datetime()

    # IMPORTANT: Table/Doctype name here matches your current code.
    # If you store e-Factura fields on Sales Invoice instead, change "eFactura".
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
            AND ef_status = %(draft)s
            AND (ef_series IS NULL OR ef_series = '')
            AND (ef_number IS NULL OR ef_number = '')
        ORDER BY
            CASE
                WHEN last_status_check IS NULL THEN 0
                ELSE 1
            END,
            last_status_check ASC
        LIMIT %(limit)s
        """,
        {"draft": DRAFT, "limit": BATCH_SIZE},
        as_dict=True,
    )

    if not docs:
        return

    client = EFacturaAPIClient.from_settings()

    total = len(docs)
    updated = 0
    unchanged = 0
    missing_in_api = 0
    multiple_found = 0
    errors = 0

    now_ts = now_datetime()

    # Keep a short sample in logs
    sample_missing = []
    sample_multi = []

    # List of statuses to check in sequence (eFactura API requires status filter)
    search_statuses = [0,1,7,8,3,2,5,6,10,4,6,9];

    for row in docs:
        try:
            
            for status in search_statuses:
                params = {
                    "APIeInvoiceId": "EF-2026-00017", 
                    "InvoiceStatus": status,                 
                }

                resp = client.search_invoices(actor_role=1, parameters=params)
                inv = _extract_single_invoice_from_search_response(resp)
                
                if inv:
                    break

            resp = client.search_invoices(actor_role=1, parameters=params)
            inv = _extract_single_invoice_from_search_response(resp)

            if inv is None:
                missing_in_api += 1
                if len(sample_missing) < 5:
                    sample_missing.append(row.name)
                continue

            if isinstance(inv, list):
                multiple_found += 1
                if len(sample_multi) < 5:
                    sample_multi.append(row.name)
                continue

            remote_series = (inv.get("Seria") or "").strip()
            remote_number = (inv.get("Number") or "").strip()
            remote_status = inv.get("InvoiceStatus")

            try:
                remote_status_code = int(remote_status) if remote_status is not None else None
            except Exception:
                remote_status_code = None

            doc = frappe.get_doc("eFactura", row.name)

            changed = False

            # Set series/number if available
            if remote_series:
                doc.db_set("ef_series", remote_series, update_modified=False)
                changed = True

            if remote_number:
                doc.db_set("ef_number", remote_number, update_modified=False)
                changed = True

            # Update status if present and different
            if remote_status_code is not None and int(doc.ef_status or 0) != remote_status_code:
                doc.db_set("ef_status", remote_status_code, update_modified=False)
                doc.set_status()
                changed = True

            # Always touch last_status_check so we don't re-check too aggressively
            doc.db_set("last_status_check", now_ts, update_modified=False)

            if changed:
                updated += 1
            else:
                unchanged += 1

        except Exception:
            errors += 1

    if missing_in_api or multiple_found or errors:
        msg_lines = [
            f"Started at: {started_at}",
            f"Batch size: {total}",
            f"Updated: {updated}",
            f"Unchanged: {unchanged}",
            f"Missing in API response: {missing_in_api}",
            f"Multiple found in API response: {multiple_found}",
            f"Errors: {errors}",
        ]
        if sample_missing:
            msg_lines.append(f"Missing (sample): {', '.join(sample_missing)}")
        if sample_multi:
            msg_lines.append(f"Multiple (sample): {', '.join(sample_multi)}")

        frappe.log_error(
            title="eFactura draft sync by APIInvoiceId summary (with issues)",
            message="\n".join(msg_lines),
        )


def _extract_single_invoice_from_search_response(resp: dict):
    """Return a single invoice dict from SearchInvoices response.

    Returns:
    - dict: when exactly one invoice is present
    - None: when no invoices
    - list: when multiple invoices (signals caller to treat as anomaly)
    """
    if not isinstance(resp, dict):
        return None

    results = resp.get("Results") or resp
    invoices = results.get("Invoice") if isinstance(results, dict) else None

    if not invoices:
        return None

    if isinstance(invoices, dict):
        return invoices

    if isinstance(invoices, list):
        if len(invoices) == 1:
            return invoices[0]
        return invoices

    return None
