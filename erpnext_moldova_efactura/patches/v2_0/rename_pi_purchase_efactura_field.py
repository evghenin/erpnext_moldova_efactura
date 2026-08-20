import frappe
from frappe.model.utils.rename_field import rename_field

OLD_FIELD = "efactura_buyer"
NEW_FIELD = "purchase_efactura"
OLD_CF = "Purchase Invoice-efactura_buyer"
NEW_CF = "Purchase Invoice-purchase_efactura"


def execute():
	"""Rename Purchase Invoice.efactura_buyer → purchase_efactura."""
	_rename_custom_field()

	has_old = frappe.db.has_column("Purchase Invoice", OLD_FIELD)
	has_new = frappe.db.has_column("Purchase Invoice", NEW_FIELD)

	if has_old and not has_new:
		rename_field("Purchase Invoice", OLD_FIELD, NEW_FIELD)
		has_old = frappe.db.has_column("Purchase Invoice", OLD_FIELD)
		has_new = frappe.db.has_column("Purchase Invoice", NEW_FIELD)

	if has_old and has_new:
		frappe.db.sql(
			"""
			UPDATE `tabPurchase Invoice`
			SET `purchase_efactura` = `efactura_buyer`
			WHERE IFNULL(`purchase_efactura`, '') = ''
				AND IFNULL(`efactura_buyer`, '') != ''
			"""
		)
		frappe.db.sql_ddl("ALTER TABLE `tabPurchase Invoice` DROP COLUMN `efactura_buyer`")


def _rename_custom_field():
	if frappe.db.exists("Custom Field", OLD_CF):
		if frappe.db.exists("Custom Field", NEW_CF):
			frappe.delete_doc("Custom Field", OLD_CF, force=1)
		else:
			cf = frappe.get_doc("Custom Field", OLD_CF)
			cf.fieldname = NEW_FIELD
			cf.label = "Purchase eFactura"
			cf.options = "Purchase eFactura"
			cf.save()
			if cf.name != NEW_CF:
				frappe.rename_doc("Custom Field", cf.name, NEW_CF, force=True)
	elif frappe.db.exists("Custom Field", NEW_CF):
		frappe.db.set_value(
			"Custom Field",
			NEW_CF,
			{
				"fieldname": NEW_FIELD,
				"label": "Purchase eFactura",
				"options": "Purchase eFactura",
			},
			update_modified=False,
		)
