frappe.listview_settings["Purchase Factura"] = {
	onload(listview) {
		// Frappe paints a checkbox-only skeleton row before reportview.get.
		// If that first refresh is throttled/skipped, the placeholder stays until Reload List.
		listview.$result
			?.find(".render-list-checkbox")
			.closest(".list-row-container")
			.remove();
		if (!listview.data?.length) {
			listview.toggle_result_area();
		}

		const pf = frappe.provide("erpnext_moldova_efactura.pf");
		if (!frappe.model.can_create("Purchase Factura") || !pf.bind_import_buttons) {
			return;
		}
		pf.bind_import_buttons((label, action) => {
			listview.page.add_inner_button(label, action, __("Import"));
		});
	},
	refresh(listview) {
		listview.$result
			?.find(".render-list-checkbox")
			.closest(".list-row-container")
			.remove();
	},
};
