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
		pf_bind_list_import(listview);
	},
	refresh(listview) {
		listview.$result
			?.find(".render-list-checkbox")
			.closest(".list-row-container")
			.remove();
		pf_bind_list_import(listview);
	},
};

function pf_bind_list_import(listview) {
	if (listview._pf_import_bound || !listview.can_create) {
		return;
	}
	const bind = () => {
		const pf = frappe.provide("erpnext_moldova_efactura.pf");
		if (!pf.bind_import_buttons) {
			return false;
		}
		pf.bind_import_buttons((label, action) => {
			listview.page.add_inner_button(label, action, __("Import"));
		});
		listview._pf_import_bound = true;
		return true;
	};
	if (bind()) {
		return;
	}
	frappe.require("/assets/erpnext_moldova_efactura/js/purchase_factura_import_v3.js", () => {
		bind();
	});
}
