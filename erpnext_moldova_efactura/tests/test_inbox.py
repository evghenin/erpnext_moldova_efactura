# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import unittest
from types import SimpleNamespace

from erpnext_moldova_efactura.inbox import rule_matches


def _rule(**kwargs):
	values = {
		"sender_name": "",
		"sender_email": "",
		"subject": "",
		"file_name": "",
		"import_via_pdf": 1,
		"import_via_ai": 0,
	}
	values.update(kwargs)
	return SimpleNamespace(**values)


class TestInboxRules(unittest.TestCase):
	def test_empty_patterns_do_not_match(self):
		self.assertFalse(rule_matches(_rule(), "Metro", "a@b.md", "Factura", "a.pdf"))

	def test_all_filled_patterns_must_match(self):
		rule = _rule(sender_email="%@metro.md", file_name="%.pdf")
		self.assertTrue(rule_matches(rule, "", "docs@metro.md", "FW: bill", "scan.PDF"))
		self.assertFalse(rule_matches(rule, "", "docs@other.md", "FW: bill", "scan.pdf"))

	def test_rule_without_a_handler_does_not_match(self):
		rule = _rule(subject="%factura%", import_via_pdf=0, import_via_ai=0)
		self.assertFalse(rule_matches(rule, "", "", "Factura 10", "a.pdf"))
