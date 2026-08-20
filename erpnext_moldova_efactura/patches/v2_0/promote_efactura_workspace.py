import json

import frappe


def execute():
	"""Make eFactura a top-level sidebar item and drop stale Buyer routes."""
	_promote_parent()
	_align_child_titles()
	_fix_hub_shortcuts()
	_rewrite_stale_routes()


def _promote_parent():
	if not frappe.db.exists("Workspace", "eFactura"):
		return
	frappe.db.set_value("Workspace", "eFactura", "parent_page", "", update_modified=False)
	frappe.db.set_value("Workspace", "eFactura", "sequence_id", 3.0, update_modified=False)


def _align_child_titles():
	"""Sidebar href uses slug(title); title must match the workspace name."""
	for name, sequence_id in (("eFactura Sales", 3.1), ("eFactura Purchase", 3.2)):
		if not frappe.db.exists("Workspace", name):
			continue
		frappe.db.set_value("Workspace", name, "title", name, update_modified=False)
		frappe.db.set_value("Workspace", name, "parent_page", "eFactura", update_modified=False)
		frappe.db.set_value("Workspace", name, "sequence_id", sequence_id, update_modified=False)


def _fix_hub_shortcuts():
	if not frappe.db.table_exists("Workspace Shortcut"):
		return
	for label, url in (("Sales", "/app/efactura-sales"), ("Purchase", "/app/efactura-purchase")):
		frappe.db.sql(
			"""
			UPDATE `tabWorkspace Shortcut`
			SET type = 'URL', url = %s, link_to = NULL
			WHERE parent = 'eFactura' AND label = %s
			""",
			(url, label),
		)


def _rewrite_stale_routes():
	if not frappe.db.table_exists("Route History"):
		return
	replacements = (
		("eFactura Buyer", "Purchase eFactura"),
		("Workspaces/Moldova eFactura", "Workspaces/eFactura"),
		("efactura-buyer", "purchase-efactura"),
		("moldova-efactura", "efactura"),
	)
	for old, new in replacements:
		frappe.db.sql(
			"""
			UPDATE `tabRoute History`
			SET route = REPLACE(route, %s, %s)
			WHERE route LIKE %s
			""",
			(old, new, f"%{old}%"),
		)
	# Bare outgoing list: List/eFactura/List, not Settings / Supplier Item Map / …
	frappe.db.sql(
		"""
		UPDATE `tabRoute History`
		SET route = 'List/Sales eFactura/List'
		WHERE route IN ('List/eFactura/List', 'List/eFactura')
		"""
	)
	_rewrite_user_recent()


def _rewrite_user_recent():
	if not frappe.db.has_column("User", "recent"):
		return
	renames = {
		"eFactura Buyer": "Purchase eFactura",
		"eFactura": "Sales eFactura",
	}
	for name, recent in frappe.db.sql("SELECT name, recent FROM `tabUser` WHERE IFNULL(recent, '') != ''"):
		try:
			data = json.loads(recent)
		except (TypeError, ValueError):
			continue
		if not isinstance(data, list):
			continue
		changed = False
		new_data = []
		for row in data:
			if not (isinstance(row, list) and row):
				new_data.append(row)
				continue
			doctype = row[0]
			if doctype in renames:
				row = [renames[doctype], *row[1:]]
				changed = True
			new_data.append(row)
		if changed:
			frappe.db.set_value("User", name, "recent", json.dumps(new_data), update_modified=False)
