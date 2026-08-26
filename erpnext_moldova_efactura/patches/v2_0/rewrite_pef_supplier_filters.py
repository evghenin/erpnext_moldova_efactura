import frappe


def execute():
	"""Replace leftover Purchase eFactura.supplier filters after the field rename."""
	_rewrite_number_cards()
	_rewrite_workspace_shortcuts()
	_rewrite_list_view_settings()


def _rewrite_number_cards():
	if not frappe.db.table_exists("Number Card") or not frappe.db.has_column("Number Card", "filters_json"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabNumber Card`
		SET filters_json = REPLACE(filters_json, '"supplier"', '"supplier_party"')
		WHERE document_type = 'Purchase eFactura'
			AND IFNULL(filters_json, '') LIKE '%"supplier"%'
		"""
	)


def _rewrite_workspace_shortcuts():
	if not frappe.db.table_exists("Workspace Shortcut"):
		return
	if frappe.db.has_column("Workspace Shortcut", "stats_filter"):
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace Shortcut`
			SET stats_filter = REPLACE(stats_filter, '"supplier"', '"supplier_party"')
			WHERE link_to = 'Purchase eFactura'
				AND IFNULL(stats_filter, '') LIKE '%"supplier"%'
			"""
		)


def _rewrite_list_view_settings():
	if not frappe.db.table_exists("List View Settings") or not frappe.db.has_column(
		"List View Settings", "fields"
	):
		return
	frappe.db.sql(
		"""
		UPDATE `tabList View Settings`
		SET fields = REPLACE(fields, '"supplier"', '"supplier_party"')
		WHERE name = 'Purchase eFactura'
			AND IFNULL(fields, '') LIKE '%"supplier"%'
		"""
	)
