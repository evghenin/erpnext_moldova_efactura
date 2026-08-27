import frappe

from erpnext_moldova_efactura.utils.party import get_customer_idno_field, get_supplier_idno_field

SEF = "Sales eFactura"


def execute():
	"""Restore Sales eFactura customer_party wiped by migrate_from_v1 on 2.1.

	That patch dropped ``customer_party`` as a v1 leftover after 2.1 JSON put
	the live party field back on that name. Fill empty Customer parties from
	the linked Sales Invoice (header, item, or SI.sales_efactura), then by
	buyer IDNO. Non-Transfer rows get Supplier by IDNO — SI.customer is a
	Customer, not a Supplier.
	"""
	if not frappe.db.table_exists(SEF):
		return
	if not frappe.db.has_column(SEF, "customer_party"):
		return
	if not frappe.db.table_exists("Sales Invoice"):
		return

	_copy_leftover_customer_column()
	_restore_from_header_si()
	_restore_from_item_si()
	_restore_from_si_link()
	_restore_from_customer_idno()
	_restore_from_supplier_idno()
	_fill_empty_party_type()


def _customer_party_filter(alias="sef"):
	"""Only fill Transfer / Customer parties. SI.customer is never a Supplier."""
	parts = []
	if frappe.db.has_column(SEF, "customer_party_type"):
		parts.append(f"ifnull({alias}.customer_party_type, '') IN ('', 'Customer')")
	if frappe.db.has_column(SEF, "type"):
		parts.append(f"ifnull({alias}.type, '') != 'Non-Transfer'")
	return " AND ".join(parts) if parts else "1=1"


def _copy_leftover_customer_column():
	if not frappe.db.has_column(SEF, "customer"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		SET sef.customer_party = sef.customer
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(sef.customer, '') != ''
			AND {_customer_party_filter()}
		"""
	)


def _restore_from_header_si():
	if not frappe.db.has_column(SEF, "sales_invoice"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		INNER JOIN `tabSales Invoice` si ON si.name = sef.sales_invoice
		SET sef.customer_party = si.customer
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(sef.sales_invoice, '') != ''
			AND ifnull(si.customer, '') != ''
			AND {_customer_party_filter()}
		"""
	)


def _restore_from_item_si():
	if not frappe.db.table_exists("Sales eFactura Item"):
		return
	if not frappe.db.has_column("Sales eFactura Item", "sales_invoice"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		INNER JOIN (
			SELECT parent, MIN(sales_invoice) AS sales_invoice
			FROM `tabSales eFactura Item`
			WHERE ifnull(sales_invoice, '') != ''
			GROUP BY parent
		) item ON item.parent = sef.name
		INNER JOIN `tabSales Invoice` si ON si.name = item.sales_invoice
		SET sef.customer_party = si.customer
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(si.customer, '') != ''
			AND {_customer_party_filter()}
		"""
	)


def _supplier_party_filter(alias="sef"):
	"""Only fill Non-Transfer / Supplier parties from Supplier IDNO."""
	parts = []
	if frappe.db.has_column(SEF, "customer_party_type"):
		parts.append(f"ifnull({alias}.customer_party_type, '') IN ('', 'Supplier')")
	if frappe.db.has_column(SEF, "type"):
		parts.append(f"ifnull({alias}.type, '') = 'Non-Transfer'")
	return " AND ".join(parts) if parts else "0=1"


def _safe_fieldname(doctype, fieldname):
	if not fieldname or not frappe.get_meta(doctype).has_field(fieldname):
		return None
	if not str(fieldname).replace("_", "").isalnum():
		return None
	return fieldname


def _restore_from_si_link():
	"""SI.sales_efactura is the reverse link when SEF.sales_invoice was never filled."""
	if not frappe.db.has_column("Sales Invoice", "sales_efactura"):
		return
	set_si = ""
	if frappe.db.has_column(SEF, "sales_invoice"):
		set_si = ", sef.sales_invoice = CASE WHEN ifnull(sef.sales_invoice, '') = '' THEN si.name ELSE sef.sales_invoice END"
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		INNER JOIN (
			SELECT si.sales_efactura, si.name, si.customer
			FROM `tabSales Invoice` si
			INNER JOIN (
				SELECT sales_efactura, MIN(name) AS name
				FROM `tabSales Invoice`
				WHERE ifnull(sales_efactura, '') != ''
					AND ifnull(customer, '') != ''
				GROUP BY sales_efactura
			) first_si ON first_si.sales_efactura = si.sales_efactura AND first_si.name = si.name
		) si ON si.sales_efactura = sef.name
		SET sef.customer_party = si.customer
			{set_si}
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(si.customer, '') != ''
			AND {_customer_party_filter()}
		"""
	)


def _restore_from_customer_idno():
	field = _safe_fieldname("Customer", get_customer_idno_field())
	if not field or not frappe.db.has_column(SEF, "ef_customer_idno"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		INNER JOIN (
			SELECT
				REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '') AS idno,
				MIN(name) AS name
			FROM `tabCustomer`
			WHERE ifnull(`{field}`, '') != ''
			GROUP BY REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '')
		) c ON c.idno = REPLACE(REPLACE(ifnull(sef.ef_customer_idno, ''), ' ', ''), '-', '')
		SET sef.customer_party = c.name
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(sef.ef_customer_idno, '') != ''
			AND {_customer_party_filter()}
		"""
	)


def _restore_from_supplier_idno():
	field = _safe_fieldname("Supplier", get_supplier_idno_field())
	if not field or not frappe.db.has_column(SEF, "ef_customer_idno"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}` sef
		INNER JOIN (
			SELECT
				REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '') AS idno,
				MIN(name) AS name
			FROM `tabSupplier`
			WHERE ifnull(`{field}`, '') != ''
			GROUP BY REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '')
		) s ON s.idno = REPLACE(REPLACE(ifnull(sef.ef_customer_idno, ''), ' ', ''), '-', '')
		SET sef.customer_party = s.name
		WHERE ifnull(sef.customer_party, '') = ''
			AND ifnull(sef.ef_customer_idno, '') != ''
			AND {_supplier_party_filter()}
		"""
	)


def _fill_empty_party_type():
	if not frappe.db.has_column(SEF, "customer_party_type"):
		return
	if frappe.db.has_column(SEF, "type"):
		frappe.db.sql(
			f"""
			UPDATE `tab{SEF}`
			SET customer_party_type = CASE
				WHEN type = 'Non-Transfer' THEN 'Supplier'
				ELSE 'Customer'
			END
			WHERE ifnull(customer_party_type, '') = ''
				AND ifnull(customer_party, '') != ''
			"""
		)
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{SEF}`
		SET customer_party_type = 'Customer'
		WHERE ifnull(customer_party_type, '') = ''
			AND ifnull(customer_party, '') != ''
		"""
	)
