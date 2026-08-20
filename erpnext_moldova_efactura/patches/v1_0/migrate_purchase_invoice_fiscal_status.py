import frappe


def execute():
	from erpnext_moldova_efactura.utils.fiscal_status import determine_pi_fiscal_status

	if not frappe.get_meta("Purchase Invoice").has_field("fiscal_status"):
		return

	names = frappe.get_all(
		"Purchase Invoice",
		filters={"docstatus": ["<", 2]},
		pluck="name",
	)
	for name in names:
		try:
			pi = frappe.get_doc("Purchase Invoice", name)
			new_status = determine_pi_fiscal_status(pi) or ""
			if (pi.get("fiscal_status") or "") == new_status:
				continue
			pi.db_set("fiscal_status", new_status, update_modified=False)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"eFactura PI fiscal_status migration failed for {name}",
			)
