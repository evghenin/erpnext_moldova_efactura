import unittest
from unittest.mock import patch

from erpnext_moldova_efactura.tasks.schedule import (
	DRAFT_JOB_TIMEOUT,
	FETCH_BUYER_JOB_ID,
	FETCH_CANCELLED_JOB_ID,
	FETCH_JOB_TIMEOUT,
	FETCH_SUPPLIER_JOB_ID,
	HOURLY_DRAFT_JOB_ID,
	HOURLY_STATUS_JOB_ID,
	HOURLY_STATUS_METHODS,
	JOB_TIMEOUT,
	STAGGER_MAX_SECONDS,
	enqueue_daily_syncs,
	enqueue_hourly_syncs,
	run_hourly_drafts,
	run_hourly_statuses,
	stagger_seconds,
)


class TestScheduleStagger(unittest.TestCase):
	def test_stagger_is_within_fifteen_minutes(self):
		with patch("erpnext_moldova_efactura.tasks.schedule.random.randint", return_value=900) as rnd:
			self.assertEqual(stagger_seconds(), 900)
			rnd.assert_called_once_with(0, STAGGER_MAX_SECONDS)
		self.assertEqual(STAGGER_MAX_SECONDS, 15 * 60)

	@patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False)
	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	def test_hourly_cron_enqueues_status_and_draft_jobs(self, enqueue, _running):
		enqueue_hourly_syncs()
		self.assertEqual(
			[call.kwargs["job_id"] for call in enqueue.call_args_list],
			[HOURLY_STATUS_JOB_ID, HOURLY_DRAFT_JOB_ID],
		)
		self.assertEqual(
			[call.args[0] for call in enqueue.call_args_list],
			[
				"erpnext_moldova_efactura.tasks.schedule.run_hourly_statuses",
				"erpnext_moldova_efactura.tasks.schedule.run_hourly_drafts",
			],
		)
		self.assertNotIn(
			"erpnext_moldova_efactura.tasks.buyer_sync.sync_buyer_invoices",
			[call.args[0] for call in enqueue.call_args_list],
		)
		enqueue.assert_any_call(
			"erpnext_moldova_efactura.tasks.schedule.run_hourly_statuses",
			queue="long",
			timeout=JOB_TIMEOUT,
			job_id=HOURLY_STATUS_JOB_ID,
			deduplicate=True,
		)
		enqueue.assert_any_call(
			"erpnext_moldova_efactura.tasks.schedule.run_hourly_drafts",
			queue="long",
			timeout=DRAFT_JOB_TIMEOUT,
			job_id=HOURLY_DRAFT_JOB_ID,
			deduplicate=True,
		)

	@patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False)
	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	def test_daily_cron_enqueues_three_jobs_sharing_fetch_ids(self, enqueue, _running):
		enqueue_daily_syncs()
		self.assertEqual(
			[call.kwargs["job_id"] for call in enqueue.call_args_list],
			[FETCH_BUYER_JOB_ID, FETCH_SUPPLIER_JOB_ID, FETCH_CANCELLED_JOB_ID],
		)
		self.assertEqual(
			[call.kwargs["timeout"] for call in enqueue.call_args_list],
			[FETCH_JOB_TIMEOUT, FETCH_JOB_TIMEOUT, JOB_TIMEOUT],
		)
		for call in enqueue.call_args_list:
			self.assertEqual(call.kwargs["queue"], "long")
			self.assertTrue(call.kwargs["deduplicate"])

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.get_attr")
	@patch("erpnext_moldova_efactura.tasks.schedule.time.sleep")
	@patch("erpnext_moldova_efactura.tasks.schedule.stagger_seconds", return_value=123)
	def test_hourly_statuses_sleep_then_call_status_methods(self, _stagger, sleep, get_attr):
		run_hourly_statuses()
		sleep.assert_called_once_with(123)
		self.assertEqual([call.args[0] for call in get_attr.call_args_list], list(HOURLY_STATUS_METHODS))
		self.assertEqual(get_attr.return_value.call_count, len(HOURLY_STATUS_METHODS))

	@patch("erpnext_moldova_efactura.tasks.status_sync.sync_efactura_draft_invoices_by_api_invoice_id")
	@patch("erpnext_moldova_efactura.tasks.schedule.time.sleep")
	@patch("erpnext_moldova_efactura.tasks.schedule.stagger_seconds", return_value=10)
	def test_hourly_drafts_are_a_separate_job(self, _stagger, sleep, draft_sync):
		run_hourly_drafts()
		sleep.assert_called_once_with(10)
		draft_sync.assert_called_once_with()
