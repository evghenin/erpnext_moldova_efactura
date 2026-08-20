import frappe


DOCTYPE_RENAMES = [
	("eFactura Item", "Sales eFactura Item"),
	("eFactura", "Sales eFactura"),
	("eFactura Buyer Item", "Purchase eFactura Item"),
	("eFactura Buyer", "Purchase eFactura"),
]


def execute():
	"""Rename outgoing/incoming eFactura doctypes before JSON sync."""
	for old, new in DOCTYPE_RENAMES:
		_rename_doctype(old, new)
	_rewrite_workspace_filters()
	_rewrite_number_cards_and_charts()
	_rename_list_view_settings()


def _rename_doctype(old: str, new: str) -> None:
	if not frappe.db.exists("DocType", old):
		return
	if frappe.db.exists("DocType", new):
		return
	frappe.rename_doc("DocType", old, new, force=True)


def _rewrite_workspace_filters() -> None:
	if not frappe.db.table_exists("Workspace Shortcut"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabWorkspace Shortcut`
		SET link_to = 'Sales eFactura'
		WHERE link_to = 'eFactura'
		"""
	)
	frappe.db.sql(
		"""
		UPDATE `tabWorkspace Shortcut`
		SET link_to = 'Purchase eFactura'
		WHERE link_to = 'eFactura Buyer'
		"""
	)
	if frappe.db.table_exists("Workspace"):
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace`
			SET content = REPLACE(content, '"eFactura"', '"Sales eFactura"')
			WHERE module = 'Moldova eFactura'
			"""
		)
	for field in ("stats_filter", "label"):
		if frappe.db.has_column("Workspace Shortcut", field):
			frappe.db.sql(
				f"""
				UPDATE `tabWorkspace Shortcut`
				SET `{field}` = REPLACE(`{field}`, 'eFactura Buyer', 'Purchase eFactura')
				WHERE IFNULL(`{field}`, '') LIKE '%eFactura Buyer%'
				"""
			)
			frappe.db.sql(
				f"""
				UPDATE `tabWorkspace Shortcut`
				SET `{field}` = REPLACE(`{field}`, '["eFactura"', '["Sales eFactura"')
				WHERE IFNULL(`{field}`, '') LIKE '%["eFactura"%'
				"""
			)
			frappe.db.sql(
				f"""
				UPDATE `tabWorkspace Shortcut`
				SET `{field}` = REPLACE(`{field}`, 'New eFactura', 'New Sales eFactura')
				WHERE IFNULL(`{field}`, '') = 'New eFactura'
				"""
			)


def _rewrite_number_cards_and_charts() -> None:
	if frappe.db.table_exists("Number Card") and frappe.db.has_column("Number Card", "document_type"):
		frappe.db.sql(
			"""
			UPDATE `tabNumber Card`
			SET document_type = 'Sales eFactura'
			WHERE document_type = 'eFactura'
			"""
		)
	if frappe.db.table_exists("Dashboard Chart") and frappe.db.has_column("Dashboard Chart", "document_type"):
		frappe.db.sql(
			"""
			UPDATE `tabDashboard Chart`
			SET document_type = 'Sales eFactura'
			WHERE document_type = 'eFactura'
			"""
		)


def _rename_list_view_settings() -> None:
	if frappe.db.exists("List View Settings", "eFactura") and not frappe.db.exists(
		"List View Settings", "Sales eFactura"
	):
		frappe.rename_doc("List View Settings", "eFactura", "Sales eFactura", force=True)
