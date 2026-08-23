(() => {
	const existing = frappe.listview_settings["Purchase Invoice"] || {};

	const custom = {
		formatters: Object.assign({}, existing.formatters || {}, {
			fiscal_status(value, field, doc) {
				if (cint(doc.docstatus) === 2) return "";
				if (cint(doc.docstatus) !== 1) return "";
				const label = value || "Pending";

				const base = String(label).replace(/ \(Draft\)$/, "");
				const color_map = {
					Pending: "red",
					Partial: "red",
					"In Progress": "yellow",
					Completed: "green",
					"Not Required": "gray",
				};
				const color = color_map[base] || "gray";
				return `
					<span class="indicator-pill no-indicator-dot ${color}">
						${__(label)}
					</span>
				`;
			},
		}),

		onload(listview) {
			if (typeof existing.onload === "function") {
				existing.onload(listview);
			}

			listview.page.add_action_item(__("Actualize Fiscal Status"), () => {
				const selected = listview.get_checked_items();
				if (!selected.length) {
					frappe.msgprint(__("Please select at least one Purchase Invoice."));
					return;
				}

				const names = selected.map((d) => d.name);
				const total = names.length;

				frappe.show_progress(__("Actualizing Fiscal Status"), 0, total, __("Starting..."));

				const progress_handler = (data) => {
					frappe.show_progress(
						__("Actualizing Fiscal Status"),
						data.current,
						data.total,
						__("Processing {0} of {1}", [data.current, data.total])
					);
				};

				const done_handler = (data) => {
					frappe.hide_progress();
					frappe.show_alert({
						message: __("Fiscal status updated for {0} invoices.", [data.updated]),
						indicator: "green",
					});
					frappe.realtime.off("bulk_pi_fiscal_status_progress", progress_handler);
					frappe.realtime.off("bulk_pi_fiscal_status_done", done_handler);
					listview.refresh();
				};

				frappe.realtime.on("bulk_pi_fiscal_status_progress", progress_handler);
				frappe.realtime.on("bulk_pi_fiscal_status_done", done_handler);

				frappe.call({
					method: "erpnext_moldova_efactura.api.fiscal_status.start_bulk_pi_job",
					args: { names },
				});
			});
		},
	};

	frappe.listview_settings["Purchase Invoice"] = Object.assign({}, existing, custom);
})();
