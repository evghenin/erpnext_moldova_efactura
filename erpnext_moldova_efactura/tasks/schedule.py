"""Stagger e-Factura scheduled jobs off the hour.

Cron fires at minute 20. The queued job sleeps 0-15 minutes, then calls SFS.
"""

from __future__ import annotations

import random
import time

import frappe

STAGGER_MAX_SECONDS = 15 * 60
# Sleep can be 15 minutes plus API work; default long-queue timeout is too short.
JOB_TIMEOUT = 30 * 60

HOURLY_METHODS = (
	"erpnext_moldova_efactura.tasks.status_sync.sync_efactura_statuses",
	"erpnext_moldova_efactura.tasks.status_sync.sync_efactura_draft_invoices_by_api_invoice_id",
	"erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_statuses",
)

DAILY_METHODS = (
	"erpnext_moldova_efactura.tasks.status_sync.sync_efactura_cancelled_from_search_invoices",
	"erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_invoices",
	"erpnext_moldova_efactura.tasks.supplier_sync.sync_supplier_invoices",
)


def stagger_seconds() -> int:
	return random.randint(0, STAGGER_MAX_SECONDS)


def _run_methods(methods: tuple[str, ...]) -> None:
	time.sleep(stagger_seconds())
	for method in methods:
		frappe.get_attr(method)()


def run_hourly_syncs() -> None:
	_run_methods(HOURLY_METHODS)


def run_daily_syncs() -> None:
	_run_methods(DAILY_METHODS)


def enqueue_hourly_syncs() -> None:
	frappe.enqueue(
		"erpnext_moldova_efactura.tasks.schedule.run_hourly_syncs",
		queue="long",
		timeout=JOB_TIMEOUT,
		job_id="efactura-hourly-syncs",
		deduplicate=True,
	)


def enqueue_daily_syncs() -> None:
	frappe.enqueue(
		"erpnext_moldova_efactura.tasks.schedule.run_daily_syncs",
		queue="long",
		timeout=JOB_TIMEOUT,
		job_id="efactura-daily-syncs",
		deduplicate=True,
	)
