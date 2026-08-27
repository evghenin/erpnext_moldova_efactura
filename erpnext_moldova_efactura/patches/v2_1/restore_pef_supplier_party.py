import frappe

from erpnext_moldova_efactura.utils.party import get_customer_idno_field, get_supplier_idno_field

PEF = "Purchase eFactura"
PEF_ITEM = "Purchase eFactura Item"


def execute():
	"""Restore Purchase eFactura supplier_party emptied by the 2.1 field rename.

	``supplier`` became ``supplier_party``. If schema sync added the new column
	before the rename patch copied values, Party was left blank. Fill empty
	Supplier parties from a leftover ``supplier`` column, linked PI / PR / PO,
	then by supplier IDNO. Return parties are filled from the linked Delivery
	Note or customer IDNO — PI.supplier is never a Customer.
	"""
	if not frappe.db.table_exists(PEF):
		return
	if not frappe.db.has_column(PEF, "supplier_party"):
		return

	_copy_leftover_supplier_column()
	_restore_from_item_link("purchase_invoice", "Purchase Invoice", "supplier", _supplier_filter)
	_restore_from_item_link("purchase_receipt", "Purchase Receipt", "supplier", _supplier_filter)
	_restore_from_reverse_link("Purchase Invoice", "supplier", _supplier_filter)
	_restore_from_reverse_link("Purchase Receipt", "supplier", _supplier_filter)
	_restore_from_reverse_link("Purchase Order", "supplier", _supplier_filter)
	_restore_from_item_link("delivery_note", "Delivery Note", "customer", _customer_filter)
	_restore_from_reverse_link("Delivery Note", "customer", _customer_filter)
	_restore_from_supplier_idno()
	_restore_from_customer_idno()
	_fill_empty_party_type()


def _supplier_filter(alias="pef"):
	"""Only fill Transfer / Supplier parties. PI.supplier is never a Customer."""
	parts = []
	if frappe.db.has_column(PEF, "supplier_party_type"):
		parts.append(f"ifnull({alias}.supplier_party_type, '') IN ('', 'Supplier')")
	if frappe.db.has_column(PEF, "is_return"):
		parts.append(f"ifnull({alias}.is_return, 0) = 0")
	return " AND ".join(parts) if parts else "1=1"


def _customer_filter(alias="pef"):
	"""Only fill return / Customer parties from Delivery Note or customer IDNO."""
	parts = []
	if frappe.db.has_column(PEF, "supplier_party_type"):
		parts.append(f"ifnull({alias}.supplier_party_type, '') IN ('', 'Customer')")
	if frappe.db.has_column(PEF, "is_return"):
		parts.append(f"ifnull({alias}.is_return, 0) = 1")
	return " AND ".join(parts) if parts else "0=1"


def _safe_fieldname(doctype, fieldname):
	if not fieldname or not frappe.get_meta(doctype).has_field(fieldname):
		return None
	if not str(fieldname).replace("_", "").isalnum():
		return None
	return fieldname


def _copy_leftover_supplier_column():
	if not frappe.db.has_column(PEF, "supplier"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}` pef
		SET pef.supplier_party = pef.supplier
		WHERE ifnull(pef.supplier_party, '') = ''
			AND ifnull(pef.supplier, '') != ''
			AND {_supplier_filter()}
		"""
	)


def _restore_from_item_link(item_field, linked_doctype, party_field, party_filter):
	if not frappe.db.table_exists(PEF_ITEM):
		return
	if not frappe.db.has_column(PEF_ITEM, item_field):
		return
	if not frappe.db.table_exists(linked_doctype):
		return
	if not frappe.db.has_column(linked_doctype, party_field):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}` pef
		INNER JOIN (
			SELECT parent, MIN(`{item_field}`) AS linked
			FROM `tab{PEF_ITEM}`
			WHERE ifnull(`{item_field}`, '') != ''
			GROUP BY parent
		) item ON item.parent = pef.name
		INNER JOIN `tab{linked_doctype}` d ON d.name = item.linked
		SET pef.supplier_party = d.`{party_field}`
		WHERE ifnull(pef.supplier_party, '') = ''
			AND ifnull(d.`{party_field}`, '') != ''
			AND {party_filter()}
		"""
	)


def _restore_from_reverse_link(doctype, party_field, party_filter):
	"""doctype.purchase_efactura is the reverse link when the item field was never filled."""
	if not frappe.db.table_exists(doctype):
		return
	if not frappe.db.has_column(doctype, "purchase_efactura"):
		return
	if not frappe.db.has_column(doctype, party_field):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}` pef
		INNER JOIN (
			SELECT t.purchase_efactura, t.name, t.`{party_field}` AS party
			FROM `tab{doctype}` t
			INNER JOIN (
				SELECT purchase_efactura, MIN(name) AS name
				FROM `tab{doctype}`
				WHERE ifnull(purchase_efactura, '') != ''
					AND ifnull(`{party_field}`, '') != ''
				GROUP BY purchase_efactura
			) first_t ON first_t.purchase_efactura = t.purchase_efactura AND first_t.name = t.name
		) t ON t.purchase_efactura = pef.name
		SET pef.supplier_party = t.party
		WHERE ifnull(pef.supplier_party, '') = ''
			AND ifnull(t.party, '') != ''
			AND {party_filter()}
		"""
	)


def _restore_from_supplier_idno():
	field = _safe_fieldname("Supplier", get_supplier_idno_field())
	if not field or not frappe.db.has_column(PEF, "ef_supplier_idno"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}` pef
		INNER JOIN (
			SELECT
				REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '') AS idno,
				MIN(name) AS name
			FROM `tabSupplier`
			WHERE ifnull(`{field}`, '') != ''
			GROUP BY REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '')
		) s ON s.idno = REPLACE(REPLACE(ifnull(pef.ef_supplier_idno, ''), ' ', ''), '-', '')
		SET pef.supplier_party = s.name
		WHERE ifnull(pef.supplier_party, '') = ''
			AND ifnull(pef.ef_supplier_idno, '') != ''
			AND {_supplier_filter()}
		"""
	)


def _restore_from_customer_idno():
	field = _safe_fieldname("Customer", get_customer_idno_field())
	if not field or not frappe.db.has_column(PEF, "ef_supplier_idno"):
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}` pef
		INNER JOIN (
			SELECT
				REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '') AS idno,
				MIN(name) AS name
			FROM `tabCustomer`
			WHERE ifnull(`{field}`, '') != ''
			GROUP BY REPLACE(REPLACE(ifnull(`{field}`, ''), ' ', ''), '-', '')
		) c ON c.idno = REPLACE(REPLACE(ifnull(pef.ef_supplier_idno, ''), ' ', ''), '-', '')
		SET pef.supplier_party = c.name
		WHERE ifnull(pef.supplier_party, '') = ''
			AND ifnull(pef.ef_supplier_idno, '') != ''
			AND {_customer_filter()}
		"""
	)


def _fill_empty_party_type():
	if not frappe.db.has_column(PEF, "supplier_party_type"):
		return
	if frappe.db.has_column(PEF, "is_return"):
		frappe.db.sql(
			f"""
			UPDATE `tab{PEF}`
			SET supplier_party_type = CASE
				WHEN ifnull(is_return, 0) = 1 THEN 'Customer'
				ELSE 'Supplier'
			END
			WHERE ifnull(supplier_party_type, '') = ''
				AND ifnull(supplier_party, '') != ''
			"""
		)
		return
	frappe.db.sql(
		f"""
		UPDATE `tab{PEF}`
		SET supplier_party_type = 'Supplier'
		WHERE ifnull(supplier_party_type, '') = ''
			AND ifnull(supplier_party, '') != ''
		"""
	)
