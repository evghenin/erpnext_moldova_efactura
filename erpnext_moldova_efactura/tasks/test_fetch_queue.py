import unittest
from unittest.mock import MagicMock, patch

import frappe

from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.tasks.buyer_sync import fetch_buyer_invoices, sync_buyer_invoices
from erpnext_moldova_efactura.tasks.schedule import (
	FETCH_BUYER_JOB_ID,
	FETCH_SUPPLIER_JOB_ID,
	SEARCH_READ_TIMEOUT,
)
from erpnext_moldova_efactura.tasks.supplier_sync import fetch_supplier_invoices, sync_supplier_invoices


def _creds():
	return {
		"wsdl_url": "http://example.test/?wsdl",
		"username": "user",
		"password": "secret",
		"timeout": 20,
		"verify_tls": True,
		"service_name": "Service",
		"port_name": "Port",
		"force_ipv4": True,
	}


class TestFetchQueue(unittest.TestCase):
	@patch("erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_invoices")
	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	@patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False)
	@patch("erpnext_moldova_efactura.tasks.buyer_sync.frappe.has_permission", return_value=True)
	def test_fetch_buyer_enqueues_without_running_sync(self, _perm, _running, enqueue, sync):
		result = fetch_buyer_invoices(lookback_days=7)
		self.assertEqual(result, {"status": "queued"})
		sync.assert_not_called()
		enqueue.assert_called_once_with(
			"erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_invoices",
			queue="long",
			timeout=4 * 60 * 60,
			job_id=FETCH_BUYER_JOB_ID,
			deduplicate=True,
			lookback_days=7,
		)

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	@patch("frappe.utils.background_jobs.is_job_enqueued", return_value=True)
	@patch("erpnext_moldova_efactura.tasks.buyer_sync.frappe.has_permission", return_value=True)
	def test_fetch_buyer_already_running_does_not_enqueue(self, _perm, _running, enqueue):
		self.assertEqual(fetch_buyer_invoices(), {"status": "already_running"})
		enqueue.assert_not_called()

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	@patch("frappe.utils.background_jobs.is_job_enqueued", side_effect=[True, False])
	@patch("erpnext_moldova_efactura.tasks.buyer_sync.frappe.has_permission", return_value=True)
	def test_fetch_buyer_enqueues_again_after_previous_job_finishes(self, _perm, _running, enqueue):
		self.assertEqual(fetch_buyer_invoices(), {"status": "already_running"})
		self.assertEqual(fetch_buyer_invoices(lookback_days=3), {"status": "queued"})
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], FETCH_BUYER_JOB_ID)

	@patch("erpnext_moldova_efactura.tasks.supplier_sync.sync_supplier_invoices")
	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	@patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False)
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.has_permission", return_value=True)
	def test_fetch_supplier_enqueues_without_running_sync(self, _perm, _running, enqueue, sync):
		result = fetch_supplier_invoices(lookback_days=9)
		self.assertEqual(result, {"status": "queued"})
		sync.assert_not_called()
		self.assertEqual(enqueue.call_args.kwargs["job_id"], FETCH_SUPPLIER_JOB_ID)
		self.assertEqual(enqueue.call_args.kwargs["lookback_days"], 9)
		self.assertEqual(
			enqueue.call_args.args[0],
			"erpnext_moldova_efactura.tasks.supplier_sync.sync_supplier_invoices",
		)

	@patch("erpnext_moldova_efactura.tasks.buyer_sync._sync_buyer_invoices_for_company", return_value={})
	@patch("erpnext_moldova_efactura.tasks.buyer_sync.EFacturaAPIClient.from_settings")
	@patch(
		"erpnext_moldova_efactura.tasks.buyer_sync.get_sync_targets",
		return_value=[{"company": "Best Test"}],
	)
	def test_buyer_background_search_uses_120_second_read(self, _targets, from_settings, _sync):
		sync_buyer_invoices()
		from_settings.assert_called_once_with(company="Best Test", timeout=SEARCH_READ_TIMEOUT)

	@patch("erpnext_moldova_efactura.tasks.supplier_sync._sync_supplier_invoices_for_company", return_value={})
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.EFacturaAPIClient.from_settings")
	@patch(
		"erpnext_moldova_efactura.tasks.supplier_sync.get_sync_targets",
		return_value=[{"company": "Best Test"}],
	)
	def test_supplier_background_search_uses_120_second_read(self, _targets, from_settings, _sync):
		sync_supplier_invoices()
		from_settings.assert_called_once_with(company="Best Test", timeout=SEARCH_READ_TIMEOUT)

	@patch("erpnext_moldova_efactura.api_client.EFacturaAPIClient.__init__", return_value=None)
	@patch("erpnext_moldova_efactura.utils.company_api.resolve_api_credentials", return_value=_creds())
	def test_settings_timeout_stays_when_search_override_is_omitted(self, _creds_mock, init):
		EFacturaAPIClient.from_settings("Best Test")
		self.assertEqual(init.call_args.kwargs["timeout"], 20)
		EFacturaAPIClient.from_settings("Best Test", timeout=SEARCH_READ_TIMEOUT)
		self.assertEqual(init.call_args.kwargs["timeout"], SEARCH_READ_TIMEOUT)


class TestDraftSearchClient(unittest.TestCase):
	@patch("erpnext_moldova_efactura.tasks.status_sync.frappe.log_error")
	@patch("erpnext_moldova_efactura.tasks.status_sync.now_datetime", return_value="2026-11-04")
	@patch("erpnext_moldova_efactura.tasks.status_sync.EFacturaAPIClient.from_settings")
	@patch("erpnext_moldova_efactura.tasks.status_sync.frappe.db.sql")
	def test_one_client_per_company(self, sql, from_settings, _now, _log):
		sql.return_value = [
			frappe._dict(name="SEF-1", company="Best Test"),
			frappe._dict(name="SEF-2", company="Best Test"),
			frappe._dict(name="SEF-3", company="Other"),
		]
		client = MagicMock()
		client.search_invoices.return_value = {}
		from_settings.return_value = client

		from erpnext_moldova_efactura.tasks.status_sync import sync_efactura_draft_invoices_by_api_invoice_id

		sync_efactura_draft_invoices_by_api_invoice_id()

		self.assertEqual(from_settings.call_count, 2)
		self.assertEqual(
			[call.kwargs["timeout"] for call in from_settings.call_args_list],
			[SEARCH_READ_TIMEOUT, SEARCH_READ_TIMEOUT],
		)
		self.assertEqual(client.search_invoices.call_count, 11 * 3)
