import frappe


OLD_NUMBER_CARDS = (
	"Finished",
	"Pending Registration",
	"Pending Customer Signature",
	"Rejected by Customer",
)


def execute():
	"""Replace Moldova eFactura workspace with eFactura / Sales / Purchase."""
	_rename_parent_workspace()
	_delete_obsolete_number_cards()
	_rename_sales_chart()


def _rename_parent_workspace():
	old, new = "Moldova eFactura", "eFactura"
	if frappe.db.exists("Workspace", old):
		if frappe.db.exists("Workspace", new):
			frappe.delete_doc("Workspace", old, force=1)
		else:
			frappe.rename_doc("Workspace", old, new, force=True)
			frappe.db.set_value("Workspace", new, "title", "eFactura", update_modified=False)


def _delete_obsolete_number_cards():
	for name in OLD_NUMBER_CARDS:
		if not frappe.db.exists("Number Card", name):
			continue
		document_type = frappe.db.get_value("Number Card", name, "document_type")
		if document_type in ("Sales eFactura", "eFactura"):
			frappe.delete_doc("Number Card", name, force=1)


def _rename_sales_chart():
	old, new = "Sales eFactura", "Sales eFactura Statuses"
	if frappe.db.exists("Dashboard Chart", old) and not frappe.db.exists("Dashboard Chart", new):
		frappe.rename_doc("Dashboard Chart", old, new, force=True)
	elif frappe.db.exists("Dashboard Chart", old) and frappe.db.exists("Dashboard Chart", new):
		frappe.delete_doc("Dashboard Chart", old, force=1)
	if frappe.db.exists("Dashboard Chart", "eFactura"):
		document_type = frappe.db.get_value("Dashboard Chart", "eFactura", "document_type")
		if document_type in ("Sales eFactura", "eFactura"):
			frappe.delete_doc("Dashboard Chart", "eFactura", force=1)
