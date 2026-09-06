frappe.listview_settings["Purchase Factura"] = {
	onload(listview) {
		if (frappe.model.can_create("Purchase Factura")) {
			listview.page.add_inner_button(
				__("Import Orange / ARAX PDF"),
				erpnext_moldova_efactura.pf.import_pdf
			);
		}
	},
};
