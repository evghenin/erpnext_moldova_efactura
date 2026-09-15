import unittest
from unittest.mock import patch

from erpnext_moldova_efactura.tasks.schedule import (
	DAILY_METHODS,
	HOURLY_METHODS,
	JOB_TIMEOUT,
	STAGGER_MAX_SECONDS,
	enqueue_daily_syncs,
	enqueue_hourly_syncs,
	run_hourly_syncs,
	stagger_seconds,
)


class TestScheduleStagger(unittest.TestCase):
	def test_stagger_is_within_fifteen_minutes(self):
		with patch("erpnext_moldova_efactura.tasks.schedule.random.randint", return_value=900) as rnd:
			self.assertEqual(stagger_seconds(), 900)
			rnd.assert_called_once_with(0, STAGGER_MAX_SECONDS)
		self.assertEqual(STAGGER_MAX_SECONDS, 15 * 60)

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	def test_hourly_cron_enqueues_long_job(self, enqueue):
		enqueue_hourly_syncs()
		enqueue.assert_called_once_with(
			"erpnext_moldova_efactura.tasks.schedule.run_hourly_syncs",
			queue="long",
			timeout=JOB_TIMEOUT,
			job_id="efactura-hourly-syncs",
			deduplicate=True,
		)

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.enqueue")
	def test_daily_cron_enqueues_long_job(self, enqueue):
		enqueue_daily_syncs()
		enqueue.assert_called_once_with(
			"erpnext_moldova_efactura.tasks.schedule.run_daily_syncs",
			queue="long",
			timeout=JOB_TIMEOUT,
			job_id="efactura-daily-syncs",
			deduplicate=True,
		)

	@patch("erpnext_moldova_efactura.tasks.schedule.frappe.get_attr")
	@patch("erpnext_moldova_efactura.tasks.schedule.time.sleep")
	@patch("erpnext_moldova_efactura.tasks.schedule.stagger_seconds", return_value=123)
	def test_hourly_run_sleeps_then_calls_jobs(self, _stagger, sleep, get_attr):
		run_hourly_syncs()
		sleep.assert_called_once_with(123)
		self.assertEqual([c.args[0] for c in get_attr.call_args_list], list(HOURLY_METHODS))
		self.assertEqual(get_attr.return_value.call_count, len(HOURLY_METHODS))
		self.assertEqual(len(DAILY_METHODS), 3)
