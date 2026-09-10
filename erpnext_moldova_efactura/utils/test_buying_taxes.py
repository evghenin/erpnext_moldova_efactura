import unittest

from erpnext_moldova_efactura.utils.buying_taxes import ensure_purchase_tax_row_defaults


class _Meta:
	def has_field(self, name):
		return name in ("category", "add_deduct_tax")


class _Tax:
	def __init__(self, category=None, add_deduct_tax=None):
		self.meta = _Meta()
		self.category = category
		self.add_deduct_tax = add_deduct_tax


class _Doc:
	def __init__(self, taxes):
		self.taxes = taxes

	def get(self, key):
		return self.taxes if key == "taxes" else None


class TestBuyingTaxesDefaults(unittest.TestCase):
	def test_fills_missing_purchase_tax_required_fields(self):
		tax = _Tax()
		ensure_purchase_tax_row_defaults(_Doc([tax]))
		self.assertEqual(tax.category, "Total")
		self.assertEqual(tax.add_deduct_tax, "Add")

	def test_keeps_existing_purchase_tax_values(self):
		tax = _Tax(category="Valuation", add_deduct_tax="Deduct")
		ensure_purchase_tax_row_defaults(_Doc([tax]))
		self.assertEqual(tax.category, "Valuation")
		self.assertEqual(tax.add_deduct_tax, "Deduct")
