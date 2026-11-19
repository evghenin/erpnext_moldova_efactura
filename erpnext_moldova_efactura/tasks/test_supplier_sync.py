import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from erpnext_moldova_efactura.tasks.supplier_sync import (
	_reconcile_pending_registration,
	_upsert_supplier_invoices,
)


def _pending_doc():
	doc = MagicMock()
	doc.name = "SEF-1"
	doc.company = "Co"
	doc.ef_status = "Pending Registration"
	doc.ef_series = "EBM"
	doc.ef_number = "720769"
	return doc


class TestSupplierPendingStatus(unittest.TestCase):
	@patch("erpnext_moldova_efactura.tasks.supplier_sync._fill_details", return_value=False)
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.get_doc")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.db.exists", return_value=None)
	def test_upsert_updates_pending_registration_when_number_padding_differs(
		self, _exists, get_doc, _fill
	):
		doc = _pending_doc()
		get_doc.return_value = doc

		result = _upsert_supplier_invoices(
			MagicMock(),
			"Co",
			{("EBM", "000720769"): 1},
			pending_index={("EBM", "720769"): "SEF-1"},
		)

		self.assertEqual(result["updated"], 1)
		self.assertEqual(result["created"], 0)
		get_doc.assert_called_once_with("Sales eFactura", "SEF-1")
		doc.db_set.assert_any_call("ef_number", "000720769", update_modified=False)
		doc.db_set.assert_any_call("ef_status", "Signed by Supplier", update_modified=False)
		doc.set_status.assert_called_once()

	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.get_doc")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.db.exists", return_value="SEF-1")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.get_all")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.check_invoices_status_resilient")
	def test_reconcile_pending_uses_check_invoices_status_when_search_misses(
		self, check_status, get_all, _exists, get_doc
	):
		row = SimpleNamespace(name="SEF-1", ef_series="EBM", ef_number="000720769")
		get_all.return_value = [row]
		doc = _pending_doc()
		doc.ef_number = "000720769"
		get_doc.return_value = doc
		check_status.return_value = ({("EBM", "000720769"): 1}, [])

		updated, errors = _reconcile_pending_registration(MagicMock(), "Co", {})

		self.assertEqual((updated, errors), (1, 0))
		check_status.assert_called_once()
		identifiers = check_status.call_args.args[1]
		self.assertEqual(identifiers, [{"Seria": "EBM", "Number": "000720769"}])
		doc.db_set.assert_any_call("ef_status", "Signed by Supplier", update_modified=False)
		doc.set_status.assert_called_once()

	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.get_doc")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.frappe.get_all")
	@patch("erpnext_moldova_efactura.tasks.supplier_sync.check_invoices_status_resilient")
	def test_reconcile_pending_uses_search_result_without_another_status_call(
		self, check_status, get_all, get_doc
	):
		row = SimpleNamespace(name="SEF-1", ef_series="EBM", ef_number="000720769")
		get_all.return_value = [row]
		doc = _pending_doc()
		doc.ef_number = "000720769"
		get_doc.return_value = doc

		updated, errors = _reconcile_pending_registration(
			MagicMock(),
			"Co",
			{("EBM", "000720769"): 8},
		)

		self.assertEqual((updated, errors), (1, 0))
		check_status.assert_not_called()
		doc.db_set.assert_any_call("ef_status", "Signed by Customer", update_modified=False)
