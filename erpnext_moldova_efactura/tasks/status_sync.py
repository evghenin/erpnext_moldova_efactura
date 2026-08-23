import frappe
from frappe.utils import now_datetime, add_days
from collections import defaultdict
from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.company_api import get_sync_targets
from erpnext_moldova_efactura.utils.search_windows import iter_search_invoices


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
            company,
            ef_series,
            ef_number,
            ef_status,
            last_status_check
        FROM `tabSales eFactura`
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

    grouped = defaultdict(list)
    for row in docs:
        grouped[row.company or ""].append(row)

    now_ts = now_datetime()
    total = len(docs)
    updated = 0
    unchanged = 0
    missing_count = 0
    errors = 0
    missing_docs = []

    for company, company_docs in grouped.items():
        if not company:
            frappe.log_error(
                title="eFactura batch status skipped: missing Company",
                message="\n".join(row.name for row in company_docs),
            )
            errors += len(company_docs)
            continue
        seria_and_numbers = [{"Seria": row.ef_series, "Number": row.ef_number} for row in company_docs]
        try:
            client = EFacturaAPIClient.from_settings(company=company)
            response = client.check_invoices_status(seria_and_numbers=seria_and_numbers)
        except Exception:
            frappe.log_error(
                title=f"eFactura batch status request failed company={company}",
                message=frappe.get_traceback(),
            )
            errors += len(company_docs)
            continue

        statuses = _extract_status_map(response)

        for row in company_docs:
            try:
                key = (str(row.ef_series), str(row.ef_number))
                new_status = statuses.get(key)

                if new_status is None:
                    missing_count += 1
                    if len(missing_docs) < 5:
                        missing_docs.append(f"{row.ef_series}{row.ef_number}")
                    continue

                doc = frappe.get_doc("Sales eFactura", row.name)

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
    updated = 0
    cancelled_count = 0

    for target in get_sync_targets():
        try:
            client = EFacturaAPIClient.from_settings(company=target["company"])
        except Exception:
            frappe.log_error(
                title=f"e-Factura SearchInvoices failed company={target['company']}",
                message=frappe.get_traceback(),
            )
            continue
        cancelled = []
        for inv in iter_search_invoices(
            client,
            actor_role=1,
            invoice_status=CANCELLED_BY_SUPPLIER,
            date_from=date_from,
            date_to=date_to,
            error_title=f"e-Factura SearchInvoices failed company={target['company']}",
        ):
            row = _cancelled_row_from_invoice(inv)
            if row:
                cancelled.append(row)
            if len(cancelled) >= MAX_RESULTS_PER_RUN:
                break
        if not cancelled:
            continue
        cancelled = list(dict.fromkeys(cancelled))[:MAX_RESULTS_PER_RUN]
        cancelled_count += len(cancelled)
        updated += _apply_cancelled_status_to_local_docs(cancelled, company=target["company"])

    frappe.logger().info(
        f"e-Factura cancelled sync finished. from={date_from} to={date_to} cancelled={cancelled_count} updated={updated}"
    )


def _cancelled_row_from_invoice(inv) -> tuple[str, str, int] | None:
    if not isinstance(inv, dict):
        return None
    try:
        status = int(inv.get("InvoiceStatus"))
    except (TypeError, ValueError):
        return None
    if status != CANCELLED_BY_SUPPLIER:
        return None
    seria = (inv.get("Seria") or "").strip()
    number = (inv.get("Number") or "").strip()
    if seria and number:
        return (seria, number, status)
    return None


def _apply_cancelled_status_to_local_docs(keys: list[tuple[str, str, int]], company: str | None = None) -> int:
    """
    Update local records. Adjust Doctype/fields below to match your data model.
    """
    updated = 0
    now_ts = now_datetime()

    for seria, number, status in keys:
        if status != CANCELLED_BY_SUPPLIER:
            continue

        filters = {"ef_series": seria, "ef_number": number, "docstatus": 1}
        if company:
            filters["company"] = company
        name = frappe.db.get_value("Sales eFactura", filters, "name")
        if not name:
            continue

        doc = frappe.get_doc("Sales eFactura", name)

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
    # If you store e-Factura fields on Sales Invoice instead, change "Sales eFactura".
    docs = frappe.db.sql(
        """
        SELECT
            name,
            company,
            ef_series,
            ef_number,
            ef_status,
            last_status_check
        FROM `tabSales eFactura`
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
    search_statuses = [0,1,7,8,3,2,5,10,4,6,9]

    for row in docs:
        try:
            client = EFacturaAPIClient.from_settings(company=row.company)
            inv = None
            for status in search_statuses:
                params = {
                    "APIeInvoiceId": row.name, 
                    "InvoiceStatus": status,
                }

                resp = client.search_invoices(actor_role=1, parameters=params)
                inv = _extract_single_invoice_from_search_response(resp)
                
                if inv:
                    break

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

            doc = frappe.get_doc("Sales eFactura", row.name)

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
