import frappe
from frappe.utils import flt


def execute():
	"""Move PI allocation child rows onto Purchase eFactura Item, then drop the table."""
	_migrate_allocation_rows()
	_migrate_from_pi_header()
	_delete_allocation_doctype()


def _item_dt() -> str | None:
	for name in ("eFactura Buyer Item", "Purchase eFactura Item"):
		if frappe.db.exists("DocType", name):
			return name
	return None


def _parent_dt() -> str | None:
	for name in ("eFactura Buyer", "Purchase eFactura"):
		if frappe.db.exists("DocType", name):
			return name
	return None


def _pi_link_field() -> str | None:
	if frappe.db.has_column("Purchase Invoice", "efactura_buyer"):
		return "efactura_buyer"
	if frappe.db.has_column("Purchase Invoice", "purchase_efactura"):
		return "purchase_efactura"
	return None


def _migrate_allocation_rows():
	item_dt = _item_dt()
	if not item_dt:
		return
	if not frappe.db.table_exists("eFactura Buyer PI Allocation"):
		return
	if not frappe.db.has_column(item_dt, "purchase_invoice"):
		return

	rows = frappe.db.sql(
		"""
		SELECT buyer_item, purchase_invoice, pi_detail
		FROM `tabeFactura Buyer PI Allocation`
		WHERE IFNULL(buyer_item, '') != ''
			AND IFNULL(purchase_invoice, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		if not frappe.db.exists(item_dt, row.buyer_item):
			continue
		current = frappe.db.get_value(item_dt, row.buyer_item, "purchase_invoice")
		if current:
			continue
		frappe.db.set_value(
			item_dt,
			row.buyer_item,
			{
				"purchase_invoice": row.purchase_invoice,
				"pi_detail": row.pi_detail or "",
			},
			update_modified=False,
		)


def _migrate_from_pi_header():
	item_dt = _item_dt()
	parent_dt = _parent_dt()
	link_field = _pi_link_field()
	if not item_dt or not parent_dt or not link_field:
		return
	if not frappe.db.has_column(item_dt, "purchase_invoice"):
		return

	invoices = frappe.db.sql(
		f"""
		SELECT name, `{link_field}` AS buyer
		FROM `tabPurchase Invoice`
		WHERE IFNULL(`{link_field}`, '') != ''
		""",
		as_dict=True,
	)
	for pi_row in invoices:
		if not frappe.db.exists(parent_dt, pi_row.buyer):
			continue
		already = frappe.db.exists(
			item_dt,
			{"parent": pi_row.buyer, "purchase_invoice": pi_row.name},
		)
		if already:
			continue
		buyer = frappe.get_doc(parent_dt, pi_row.buyer)
		pi = frappe.get_doc("Purchase Invoice", pi_row.name)
		if not buyer.items or not pi.items or len(buyer.items) != len(pi.items):
			continue
		for brow, prow in zip(buyer.items, pi.items):
			if brow.purchase_invoice or not brow.name or not prow.name:
				continue
			if not eq_qty(brow, prow):
				continue
			frappe.db.set_value(
				item_dt,
				brow.name,
				{
					"purchase_invoice": pi.name,
					"pi_detail": prow.name,
				},
				update_modified=False,
			)


def eq_qty(brow, prow) -> bool:
	bqty = flt(brow.qty) if flt(brow.qty) else flt(brow.ef_qty)
	return flt(bqty, 3) == flt(prow.qty, 3)


def _delete_allocation_doctype():
	if frappe.db.exists("DocType", "eFactura Buyer PI Allocation"):
		frappe.delete_doc("DocType", "eFactura Buyer PI Allocation", force=1)
