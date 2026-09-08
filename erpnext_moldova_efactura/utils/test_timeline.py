# Copyright (c) 2026, Evgheni Nemerenco and Contributors
# See license.txt

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_moldova_efactura.utils.timeline import EF_LOG_PREFIX, log_event


class TestTimeline(FrappeTestCase):
	def test_log_event_stores_msgid_pattern_as_info(self):
		doc = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "efactura timeline test",
			}
		).insert(ignore_permissions=True)
		try:
			log_event(
				doc,
				"assigned series and number {0}{1} for signing this document",
				"EBL",
				"1",
			)
			rows = frappe.get_all(
				"Comment",
				filters={"reference_doctype": "ToDo", "reference_name": doc.name},
				fields=["comment_type", "content"],
			)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0].comment_type, "Info")
			self.assertTrue(rows[0].content.startswith(EF_LOG_PREFIX))
			payload = json.loads(rows[0].content[len(EF_LOG_PREFIX) :])
			self.assertEqual(
				payload["msgid"],
				"assigned series and number {0}{1} for signing this document",
			)
			self.assertEqual(payload["args"], ["EBL", "1"])
			self.assertFalse(
				frappe.db.exists(
					"Comment",
					{
						"reference_doctype": "ToDo",
						"reference_name": doc.name,
						"comment_type": "Comment",
					},
				)
			)
		finally:
			frappe.delete_doc("ToDo", doc.name, force=1, ignore_permissions=True)

	def test_log_event_stores_translate_args(self):
		doc = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": "efactura timeline translate_args",
			}
		).insert(ignore_permissions=True)
		try:
			log_event(doc, "linked {0} {1}", "Purchase Receipt", "PR-1", translate_args=[0])
			content = frappe.db.get_value(
				"Comment",
				{"reference_doctype": "ToDo", "reference_name": doc.name, "comment_type": "Info"},
				"content",
			)
			payload = json.loads(content[len(EF_LOG_PREFIX) :])
			self.assertEqual(payload["translate_args"], [0])
			self.assertEqual(payload["args"], ["Purchase Receipt", "PR-1"])
		finally:
			frappe.delete_doc("ToDo", doc.name, force=1, ignore_permissions=True)
