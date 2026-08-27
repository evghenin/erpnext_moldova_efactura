import frappe
from frappe.utils import cint

from erpnext_moldova_efactura.utils.party import find_customer_by_idno, find_supplier_by_idno

SEF = "Sales eFactura"


def execute():
	"""Non-Transfer Sales eFactura: Customer unless marked as return (then Supplier)."""
	if not frappe.db.table_exists(SEF):
		return
	if not frappe.db.has_column(SEF, "customer_party_type"):
		return
	if not frappe.db.has_column(SEF, "type"):
		return

	has_return = frappe.db.has_column(SEF, "is_return")
	return_sql = "ifnull(is_return, 0)" if has_return else "0"

	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}`
		SET customer_party_type = CASE
			WHEN type = 'Non-Transfer' AND {return_sql} = 1 THEN 'Supplier'
			ELSE 'Customer'
		END
		WHERE type = 'Non-Transfer'
			AND ifnull(customer_party_type, '') != CASE
				WHEN type = 'Non-Transfer' AND {return_sql} = 1 THEN 'Supplier'
				ELSE 'Customer'
			END
		"""
	)

	_retarget_parties()


def _retarget_parties():
	if not frappe.db.has_column(SEF, "customer_party"):
		return
	if not frappe.db.has_column(SEF, "ef_customer_idno"):
		return

	rows = frappe.db.sql(
		f"""
		SELECT name, customer_party_type, customer_party, ef_customer_idno, is_return
		FROM `tab{SEF}`
		WHERE type = 'Non-Transfer'
		""",
		as_dict=True,
	)
	for row in rows:
		want = "Supplier" if cint(row.is_return) else "Customer"
		current = (row.customer_party or "").strip()
		if want == "Supplier":
			if current and frappe.db.exists("Supplier", current):
				continue
			found = find_supplier_by_idno(row.ef_customer_idno)
			frappe.db.set_value(
				SEF,
				row.name,
				{"customer_party_type": want, "customer_party": found or ""},
				update_modified=False,
			)
			continue
		if current and frappe.db.exists("Customer", current):
			if row.customer_party_type != "Customer":
				frappe.db.set_value(SEF, row.name, "customer_party_type", "Customer", update_modified=False)
			continue
		found = find_customer_by_idno(row.ef_customer_idno)
		frappe.db.set_value(
			SEF,
			row.name,
			{"customer_party_type": "Customer", "customer_party": found or ""},
			update_modified=False,
		)
