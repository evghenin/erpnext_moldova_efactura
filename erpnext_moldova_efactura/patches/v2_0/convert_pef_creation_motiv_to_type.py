import frappe


def execute():
	"""Map Purchase eFactura.creation_motiv (4/5) onto type (Transfer / Non-Transfer)."""
	if not frappe.db.table_exists("Purchase eFactura"):
		return
	if not frappe.db.has_column("Purchase eFactura", "creation_motiv"):
		return
	if not frappe.db.has_column("Purchase eFactura", "type"):
		frappe.db.sql(
			"ALTER TABLE `tabPurchase eFactura` ADD COLUMN `type` varchar(140) DEFAULT 'Transfer'"
		)
	frappe.db.sql(
		"""
		UPDATE `tabPurchase eFactura`
		SET `type` = CASE
			WHEN CAST(creation_motiv AS CHAR) IN ('5', '5.0') THEN 'Non-Transfer'
			ELSE 'Transfer'
		END
		"""
	)
