frappe.listview_settings["Purchase Factura"] = {
	onload(listview) {
		if (frappe.model.can_create("Purchase Factura")) {
			listview.page.add_inner_button(
				__("Any Image with AI"),
				erpnext_moldova_efactura.pf.import_image,
				__("Import")
			);
			listview.page.add_inner_button(
				__("PDF Orange / Arax"),
				erpnext_moldova_efactura.pf.import_pdf,
				__("Import")
			);
		}
	},
};
