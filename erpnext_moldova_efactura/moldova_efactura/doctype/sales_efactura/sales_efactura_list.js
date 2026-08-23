frappe.listview_settings["Sales eFactura"] = {
	hide_name_column: false,
	add_fields: ["status", "total", "customer", "sales_invoice"],
	filters: [["status", "not in", ["Cancelled", "Canceled by Supplier"]]],
	get_indicator(doc) {
		const colors = {
			Draft: "gray",
			Cancelled: "darkgrey",
			"Pending Registration": "orange",
			"Registered as Draft": "orange",
			"Signed by Supplier": "blue",
			"Rejected by Customer": "red",
			"Accepted by Customer": "yellow",
			"Canceled by Supplier": "darkgrey",
			"Sent to Customer": "yellow",
			"Signed by Customer": "green",
			Transportation: "blue",
			"Cancellation Requested": "purple",
		};
		const status = doc.status || "";
		return [__(status), colors[status] || "gray", "status,=," + status];
	},
	onload(listview) {
		if (!frappe.model.can_write("Sales eFactura")) {
			return;
		}

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
