frappe.listview_settings["Sales eFactura"] = {
	hide_name_column: false,
	add_fields: ["status", "ef_status", "total", "customer_party", "sales_invoice", "is_return"],
	filters: [
		["status", "not in", ["Cancelled"]],
		["ef_status", "not in", ["Canceled by Supplier"]],
	],
	get_indicator(doc) {
		if (cint(doc.docstatus) === 1 && cint(doc.is_return) === 1) {
			return [__("Return"), "gray", "status,=,Return"];
		}
		if (cint(doc.docstatus) === 2) {
			return [__("Cancelled"), "red", "docstatus,=,2"];
		}
		if (cint(doc.docstatus) === 1) {
			return [__("Submitted"), "blue", "docstatus,=,1"];
		}
		return [__("Draft"), "red", "docstatus,=,0"];
	},
	formatters: {
		ef_status(value) {
			if (!value) {
				return "";
			}
			const colors = {
				"Pending Registration": "orange",
				"Registered as Draft": "orange",
				"Signed by Supplier": "blue",
				"Rejected by Customer": "red",
				"Accepted by Customer": "yellow",
				"Canceled by Supplier": "darkgrey",
				Archived: "darkgrey",
				"Sent to Customer": "yellow",
				"Signed by Customer": "green",
				Transportation: "blue",
				"Cancellation Requested": "purple",
			};
			const color = colors[value] || "gray";
			return `<span class="indicator-pill no-indicator-dot ${color}">${frappe.utils.escape_html(__(value))}</span>`;
		},
	},
	onload(listview) {
		if (!frappe.model.can_write("Sales eFactura")) {
			return;
		}

		listview.page.add_action_item(__("Register Signed"), async () => {
			const selected = listview.get_checked_items();
			if (!selected.length) {
				frappe.msgprint(__("Please select at least one Sales eFactura."));
				return;
			}
			try {
				const result = await erpnext_moldova_efactura.moldsign.bulk_sign_sales_efactura(
					selected.map((d) => d.name)
				);
				if (result) {
					listview.refresh();
				}
			} catch (e) {
				frappe.hide_progress();
				frappe.msgprint({
					title: __("Signing error"),
					indicator: "red",
					message: e.message || String(e),
				});
			}
		});

		listview.page.add_action_item(__("Register Unsigned"), async () => {
			const selected = listview.get_checked_items();
			if (!selected.length) {
				frappe.msgprint(__("Please select at least one Sales eFactura."));
				return;
			}
			try {
				const result =
					await erpnext_moldova_efactura.moldsign.bulk_register_unsigned_sales_efactura(
						selected.map((d) => d.name)
					);
				if (result) {
					listview.refresh();
				}
			} catch (e) {
				frappe.hide_progress();
				frappe.msgprint({
					title: __("Register Unsigned"),
					indicator: "red",
					message: e.message || String(e),
				});
			}
		});

		listview.page.add_inner_button(__("Fetch from e-Factura"), () => {
			frappe.prompt(
				[
					{
						fieldname: "lookback_days",
						fieldtype: "Int",
						label: __("Lookback days"),
						default: 180,
						reqd: 1,
					},
				],
				(values) => {
					frappe.call({
						method: "erpnext_moldova_efactura.tasks.supplier_sync.fetch_supplier_invoices",
						args: { lookback_days: values.lookback_days },
						freeze: true,
						freeze_message: __("Fetching supplier invoices..."),
						callback(r) {
							if (r.message) {
								frappe.msgprint({
									title: __("Fetch complete"),
									message: __(
										"Found {0}, created {1}, updated {2}, skipped {3}, details {4}, errors {5}",
										[
											r.message.found,
											r.message.created,
											r.message.updated,
											r.message.skipped || 0,
											r.message.details_loaded,
											r.message.errors,
										]
									),
									indicator: "green",
								});
								listview.refresh();
							}
						},
					});
				},
				__("Fetch Supplier Invoices"),
				__("Fetch")
			);
		});
	},
};
