import frappe


def execute():
	"""Keep existing PEF amounts as eFactura currency (rate 1)."""
	if not frappe.db.exists("DocType", "Purchase eFactura"):
		return

	ef_cur = frappe.db.get_single_value("eFactura Settings", "currency") or "MDL"
	if frappe.db.has_column("Purchase eFactura", "ef_currency"):
		frappe.db.sql(
			"""
			UPDATE `tabPurchase eFactura`
			SET ef_currency = %s
			WHERE ifnull(ef_currency, '') = ''
			""",
			(ef_cur,),
		)
	if frappe.db.has_column("Purchase eFactura", "ef_conversion_rate"):
		frappe.db.sql(
			"""
			UPDATE `tabPurchase eFactura`
			SET ef_conversion_rate = 1
			WHERE ifnull(ef_conversion_rate, 0) = 0
			"""
		)
	header_pairs = (
		("ef_total", "total"),
		("ef_vat_total", "vat_total"),
		("ef_net_total", "net_total"),
	)
	for ef_field, doc_field in header_pairs:
		if frappe.db.has_column("Purchase eFactura", ef_field) and frappe.db.has_column(
			"Purchase eFactura", doc_field
		):
			frappe.db.sql(
				f"""
				UPDATE `tabPurchase eFactura`
				SET `{ef_field}` = `{doc_field}`
				WHERE ifnull(`{ef_field}`, 0) = 0 AND ifnull(`{doc_field}`, 0) != 0
				"""
			)

	if not frappe.db.exists("DocType", "Purchase eFactura Item"):
		return
	item_pairs = (
		("ef_rate", "rate"),
		("ef_rate_with_vat", "rate_with_vat"),
		("ef_amount", "amount"),
		("ef_net_amount", "net_amount"),
		("ef_vat_amount", "vat_amount"),
	)
	for ef_field, doc_field in item_pairs:
		if frappe.db.has_column("Purchase eFactura Item", ef_field) and frappe.db.has_column(
			"Purchase eFactura Item", doc_field
		):
			frappe.db.sql(
				f"""
				UPDATE `tabPurchase eFactura Item`
				SET `{ef_field}` = `{doc_field}`
				WHERE ifnull(`{ef_field}`, 0) = 0 AND ifnull(`{doc_field}`, 0) != 0
				"""
			)

	frappe.clear_cache(doctype="Purchase eFactura")
