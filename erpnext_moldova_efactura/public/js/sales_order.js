frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const name = frm.doc.sales_efactura;
		if (!name || !frappe.model.can_read("Sales eFactura")) {
			return;
		}
		frm.add_custom_button(
			name,
			() => {
				frappe.model.clear_doc("Sales eFactura", name);
				frappe.set_route("Form", "Sales eFactura", name);
			},
			__("Sales eFactura")
		);
	},
});
