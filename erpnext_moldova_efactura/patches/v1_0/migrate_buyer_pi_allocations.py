import frappe
from frappe.utils import flt


def execute():
	"""Move header/custom-field PI links into eFactura Buyer PI Allocation rows."""
	_migrate_header_purchase_invoice()
	_migrate_pi_custom_field()
	_delete_custom_fields()
	_delete_po_child_doctype()


def _migrate_header_purchase_invoice():
	if not frappe.db.exists("DocType", "eFactura Buyer"):
		return
	if not frappe.db.has_column("eFactura Buyer", "purchase_invoice"):
		return
	if not frappe.db.exists("DocType", "eFactura Buyer PI Allocation"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, purchase_invoice
		FROM `tabeFactura Buyer`
		WHERE IFNULL(purchase_invoice, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		_allocate_one_to_one(row.name, row.purchase_invoice)


def _migrate_pi_custom_field():
	if not frappe.db.exists("DocType", "Purchase Invoice"):
		return
	if not frappe.db.has_column("Purchase Invoice", "efactura_buyer"):
		return
	if not frappe.db.exists("DocType", "eFactura Buyer PI Allocation"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, efactura_buyer
		FROM `tabPurchase Invoice`
		WHERE IFNULL(efactura_buyer, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		_allocate_one_to_one(row.efactura_buyer, row.name)


def _allocate_one_to_one(buyer_name, pi_name):
	if not buyer_name or not pi_name:
		return
	if not frappe.db.exists("eFactura Buyer", buyer_name):
		return
	if not frappe.db.exists("Purchase Invoice", pi_name):
		return
	if frappe.db.exists(
		"eFactura Buyer PI Allocation",
		{"parent": buyer_name, "purchase_invoice": pi_name},
	):
		return

	buyer = frappe.get_doc("eFactura Buyer", buyer_name)
	pi = frappe.get_doc("Purchase Invoice", pi_name)
	if not buyer.items or not pi.items:
		return

	pairs = []
	if len(buyer.items) == len(pi.items):
		pairs = list(zip(buyer.items, pi.items))
	else:
		used = set()
		for brow in buyer.items:
			for prow in pi.items:
				if prow.name in used:
					continue
				if brow.item_code and prow.item_code and brow.item_code == prow.item_code:
					pairs.append((brow, prow))
					used.add(prow.name)
					break

	for brow, prow in pairs:
		if not brow.name or not prow.name:
			continue
		frappe.get_doc(
			{
				"doctype": "eFactura Buyer PI Allocation",
				"parent": buyer_name,
				"parenttype": "eFactura Buyer",
				"parentfield": "pi_allocations",
				"buyer_item": brow.name,
				"purchase_invoice": pi_name,
				"pi_detail": prow.name,
				"qty": flt(brow.qty) or flt(prow.qty),
			}
		).insert(ignore_permissions=True)

	buyer.reload()


def _delete_custom_fields():
	for name in ("Purchase Invoice-efactura_buyer", "Purchase Order-efactura_buyer"):
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, force=1)


def _delete_po_child_doctype():
	if frappe.db.exists("DocType", "eFactura Buyer Purchase Order"):
		frappe.delete_doc("DocType", "eFactura Buyer Purchase Order", force=1)
