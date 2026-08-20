frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		const name = frm.doc.purchase_efactura;
		if (!name || !frappe.model.can_read("Purchase eFactura")) {
			return;
		}
		frm.add_custom_button(
			name,
			() => {
				frappe.model.clear_doc("Purchase eFactura", name);
				frappe.set_route("Form", "Purchase eFactura", name);
			},
			__("Purchase eFactura")
		);
	},
});
