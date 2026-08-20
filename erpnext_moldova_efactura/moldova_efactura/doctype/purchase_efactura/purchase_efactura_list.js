frappe.listview_settings["Purchase eFactura"] = {
	hide_name_column: false,
	add_fields: ["status", "efactura_status", "ef_status", "supplier", "total"],
	filters: [["status", "not in", ["Canceled by Supplier"]]],
	formatters: {
		efactura_status(value) {
			if (!value) {
				return "";
			}
			const colors = {
				"Sent to Buyer": "orange",
				"Signed by Supplier": "orange",
				Accepted: "yellow",
				Rejected: "red",
				"Signed by Buyer": "green",
				Signing: "yellow",
				Transportation: "yellow",
				"Canceled by Supplier": "darkgrey",
				"Cancellation Requested": "purple",
			};
			const status = String(value).includes(" · ") ? String(value).split(" · ")[0] : String(value);
			const color = colors[status] || "gray";
			return `<span class="indicator-pill no-indicator-dot ${color}">${frappe.utils.escape_html(__(status))}</span>`;
		},
	},
	onload(listview) {
		// Incoming invoices are created only via Fetch / sync
		if (listview.page.btn_primary) {
			listview.page.btn_primary.hide();
		}
		listview.page.clear_primary_action();

		if (!frappe.model.can_write("Purchase eFactura")) {
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
						method: "erpnext_moldova_efactura.tasks.buyer_sync.fetch_buyer_invoices",
						args: { lookback_days: values.lookback_days },
						freeze: true,
						freeze_message: __("Fetching buyer invoices..."),
						callback(r) {
							if (r.message) {
								frappe.msgprint({
									title: __("Fetch complete"),
									message: __(
										"Found {0}, created {1}, updated {2}, details {3}, errors {4}",
										[
											r.message.found,
											r.message.created,
											r.message.updated,
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
				__("Fetch Buyer Invoices"),
				__("Fetch")
			);
		});
	},
};
