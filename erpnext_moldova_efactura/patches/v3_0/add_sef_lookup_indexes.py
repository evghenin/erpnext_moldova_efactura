import frappe


def execute():
	"""Composite index for unique series/number checks on SEF save.

	Single-column indexes come from DocType search_index on migrate.
	"""
	frappe.db.add_index(
		"Sales eFactura",
		["company", "ef_series", "ef_number"],
		index_name="company_series_number",
	)
