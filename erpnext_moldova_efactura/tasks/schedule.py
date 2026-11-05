"""Stagger e-Factura scheduled jobs off SFS overload windows.

SFS overloads at minute 0 of every hour and around midnight. Hourly cron fires
at minute 20. Daily cron fires at 06:20. The queued job then sleeps 0-15 minutes.
"""

from __future__ import annotations

import random
import time

import frappe

STAGGER_MAX_SECONDS = 15 * 60
# Sleep can be 15 minutes plus API work; default long-queue timeout is too short.
JOB_TIMEOUT = 30 * 60
# Buyer/supplier search is many SearchInvoices calls of up to 120 seconds, plus XML loads.
FETCH_JOB_TIMEOUT = 4 * 60 * 60
SEARCH_READ_TIMEOUT = 120
# User actions (sign, accept, reject, cancel, register) use the same read budget.
ACTION_READ_TIMEOUT = SEARCH_READ_TIMEOUT
# One hourly pass checks at most 50 drafts, each with up to 11 SearchInvoices reads.
DRAFT_JOB_TIMEOUT = 50 * 11 * SEARCH_READ_TIMEOUT + STAGGER_MAX_SECONDS

FETCH_BUYER_JOB_ID = "efactura-fetch-buyer"
FETCH_SUPPLIER_JOB_ID = "efactura-fetch-supplier"
FETCH_CANCELLED_JOB_ID = "efactura-fetch-cancelled"
HOURLY_STATUS_JOB_ID = "efactura-hourly-statuses"
HOURLY_DRAFT_JOB_ID = "efactura-hourly-drafts"

HOURLY_STATUS_METHODS = (
	"erpnext_moldova_efactura.tasks.status_sync.sync_efactura_statuses",
	"erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_statuses",
)


def stagger_seconds() -> int:
	return random.randint(0, STAGGER_MAX_SECONDS)


def enqueue_job(method: str, job_id: str, timeout: int, **kwargs) -> str:
	"""Queue one background sync. Return queued or already_running."""
	from frappe.utils.background_jobs import is_job_enqueued

	if is_job_enqueued(job_id):
		return "already_running"
	frappe.enqueue(
		method,
		queue="long",
		timeout=timeout,
		job_id=job_id,
		deduplicate=True,
		**kwargs,
	)
	return "queued"


def _run_methods(methods: tuple[str, ...]) -> None:
	time.sleep(stagger_seconds())
	for method in methods:
		frappe.get_attr(method)()


def run_scheduled_buyer_fetch() -> None:
	time.sleep(stagger_seconds())
	from erpnext_moldova_efactura.tasks.buyer_sync import sync_buyer_invoices

	sync_buyer_invoices()


def run_scheduled_supplier_fetch() -> None:
	time.sleep(stagger_seconds())
	from erpnext_moldova_efactura.tasks.supplier_sync import sync_supplier_invoices

	sync_supplier_invoices()


def run_scheduled_cancelled_fetch() -> None:
	time.sleep(stagger_seconds())
	from erpnext_moldova_efactura.tasks.status_sync import sync_efactura_cancelled_from_search_invoices

	sync_efactura_cancelled_from_search_invoices()


def run_hourly_statuses() -> None:
	_run_methods(HOURLY_STATUS_METHODS)


def run_hourly_drafts() -> None:
	time.sleep(stagger_seconds())
	from erpnext_moldova_efactura.tasks.status_sync import sync_efactura_draft_invoices_by_api_invoice_id

	sync_efactura_draft_invoices_by_api_invoice_id()


def enqueue_hourly_syncs() -> None:
	enqueue_job(
		"erpnext_moldova_efactura.tasks.schedule.run_hourly_statuses",
		HOURLY_STATUS_JOB_ID,
		JOB_TIMEOUT,
	)
	enqueue_job(
		"erpnext_moldova_efactura.tasks.schedule.run_hourly_drafts",
		HOURLY_DRAFT_JOB_ID,
		DRAFT_JOB_TIMEOUT,
	)


def enqueue_daily_syncs() -> None:
	enqueue_job(
		"erpnext_moldova_efactura.tasks.schedule.run_scheduled_buyer_fetch",
		FETCH_BUYER_JOB_ID,
		FETCH_JOB_TIMEOUT,
	)
	enqueue_job(
		"erpnext_moldova_efactura.tasks.schedule.run_scheduled_supplier_fetch",
		FETCH_SUPPLIER_JOB_ID,
		FETCH_JOB_TIMEOUT,
	)
	enqueue_job(
		"erpnext_moldova_efactura.tasks.schedule.run_scheduled_cancelled_fetch",
		FETCH_CANCELLED_JOB_ID,
		JOB_TIMEOUT,
	)
