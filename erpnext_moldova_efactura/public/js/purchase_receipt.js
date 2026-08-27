frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		const sefName = frm.doc.sales_efactura;
		if (sefName && frappe.model.can_read("Sales eFactura")) {
			frm.add_custom_button(
				sefName,
				() => {
					frappe.model.clear_doc("Sales eFactura", sefName);
					frappe.set_route("Form", "Sales eFactura", sefName);
				},
				__("Sales eFactura")
			);
		}

		if (frm.doc.docstatus === 1) {
			const status = frm.doc.fiscal_status;
			if (status) {
				const base = String(status).replace(/ \(Draft\)$/, "");
				const color_map = {
					Pending: "red",
					Partial: "red",
					"In Progress": "yellow",
					Completed: "green",
					Failed: "red",
					"Not Required": "gray",
					"Not Applicable": "gray",
					Unknown: "red",
				};
				frm.page.set_indicator(
					__("Fiscalization: {0}", [__(status)]),
					color_map[base] || "gray"
				);
			}

			frm.add_custom_button(
				__("Actualize Fiscal Status"),
				() => {
					frappe.call({
						method: "erpnext_moldova_efactura.api.fiscal_status.actualize_purchase_receipt_fiscal_status",
						args: { purchase_receipt: frm.doc.name },
						freeze: true,
						callback(r) {
							if (r.message) {
								frappe.show_alert({
									message: __("Fiscal status updated."),
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
					});
				},
				__("Actions")
			);
		}

		if (
			frm.doc.docstatus === 1 &&
			cint(frm.doc.is_return) === 1 &&
			frm.doc.return_against &&
			frappe.model.can_create("Sales eFactura") &&
			frappe.model.can_read("Purchase Receipt") &&
			!frm.doc.sales_efactura
		) {
			frappe.call({
				method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.live_sales_efactura_for_purchase_receipt",
				args: { purchase_receipt: frm.doc.name },
				callback(r) {
					if (r.message) {
						return;
					}
					frm.add_custom_button(
						__("Sales eFactura for return (Non-Transfer)"),
						() => {
							frappe.model.open_mapped_doc({
								method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.make_efactura_from_purchase_receipt_return",
								frm: frm,
							});
						},
						__("Create")
					);
				},
			});
		}
	},
});
