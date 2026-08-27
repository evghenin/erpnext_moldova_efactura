frappe.ui.form.on("Purchase Invoice", {
	onload(frm) {
		if (frm.is_new()) {
			prefill_from_linked_pef(frm);
		}
	},
	refresh(frm) {
		if (frm.is_new()) {
			prefill_from_linked_pef(frm);
			return;
		}

		if (frm.doc.docstatus === 1) {
			const status = frm.doc.fiscal_status || "Pending";
			if (status) {
				const base = String(status).replace(/ \(Draft\)$/, "");
				const color_map = {
					Pending: "red",
					Partial: "red",
					"In Progress": "yellow",
					Completed: "green",
					"Not Required": "gray",
				};
				frm.page.set_indicator(
					__("Fiscalization: {0}", [__(status)]),
					color_map[base] || "gray"
				);
			}

			const base = status ? String(status).replace(/ \(Draft\)$/, "") : "";
			const showFiscal = base && !["Pending", "Not Required"].includes(base);
			if (showFiscal) {
				frm.add_custom_button(
					__("Actualize Fiscal Status"),
					() => {
						frappe.call({
							method: "erpnext_moldova_efactura.api.fiscal_status.actualize_purchase_invoice_fiscal_status",
							args: { purchase_invoice: frm.doc.name },
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
		}

		frappe.call({
			method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.get_linked_buyers",
			args: { purchase_invoice: frm.doc.name },
			callback(r) {
				if (!frappe.model.can_read("Purchase eFactura")) {
					return;
				}
				(r.message || []).forEach((name) => {
					frm.add_custom_button(
						name,
						() => open_purchase_efactura(name),
						__("Purchase eFactura")
					);
				});
			},
		});
	},

	on_submit(frm) {
		offer_return_to_purchase_efactura(frm);
	},
});

function open_purchase_efactura(name) {
	if (!name) {
		return;
	}
	frappe.model.clear_doc("Purchase eFactura", name);
	frappe.set_route("Form", "Purchase eFactura", name);
}

function first_purchase_order(frm) {
	return (frm.doc.items || []).map((row) => row.purchase_order).find(Boolean);
}

function posting_time_from_pef(value) {
	if (!value) {
		return null;
	}
	const match = String(value).match(/(\d{1,2}:\d{2}:\d{2})/);
	return match ? match[1] : null;
}

function prefill_from_linked_pef(frm) {
	if (!frm.is_new() || frm._ef_pef_prefilled) {
		return;
	}

	const apply = (pefName) => {
		if (!pefName) {
			return;
		}
		frm._ef_pef_prefilled = true;
		if (!frm.doc.purchase_efactura) {
			frm.set_value("purchase_efactura", pefName);
		}
		frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura").then((copy) => {
			if (!cint(copy)) {
				return;
			}
			frappe.db.get_value("Purchase eFactura", pefName, ["issue_date", "issue_time"]).then((r) => {
				const data = r.message || {};
				if (!data.issue_date) {
					return;
				}
				frm.set_value("set_posting_time", 1);
				frm.set_value("posting_date", data.issue_date);
				const issueTime = posting_time_from_pef(data.issue_time);
				if (issueTime) {
					frm.set_value("posting_time", issueTime);
				}
			});
		});
	};

	if (frm.doc.purchase_efactura) {
		apply(frm.doc.purchase_efactura);
		return;
	}

	const po = first_purchase_order(frm);
	if (!po || !frappe.meta.has_field("Purchase Order", "purchase_efactura")) {
		return;
	}
	frappe.db.get_value("Purchase Order", po, "purchase_efactura").then((r) => {
		apply(r.message && r.message.purchase_efactura);
	});
}

function offer_return_to_purchase_efactura(frm) {
	if (!frm.doc || cint(frm.doc.docstatus) !== 1) {
		return;
	}
	if (!frappe.model.can_read("Purchase eFactura")) {
		return;
	}

	const ask = (name) => {
		if (!name) {
			return;
		}
		frappe.db.get_value("Purchase eFactura", name, "docstatus").then((r) => {
			if (cint(r?.message?.docstatus) !== 0) {
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Return to Purchase eFactura"),
				primary_action_label: __("Open e-Factura"),
				primary_action() {
					dialog.hide();
					open_purchase_efactura(name);
				},
				secondary_action_label: __("Stay here"),
				secondary_action() {
					dialog.hide();
				},
			});
			dialog.$body.append(
				`<p class="frappe-confirm-message">${__(
					"Purchase Invoice {0} is submitted. Open Purchase eFactura {1} to submit it?",
					[frappe.utils.escape_html(frm.doc.name), frappe.utils.escape_html(name)]
				)}</p>`
			);
			dialog.show();
		});
	};

	if (frm.doc.purchase_efactura) {
		ask(frm.doc.purchase_efactura);
		return;
	}

	const po = first_purchase_order(frm);
	const ask_from_buyers = () => {
		frappe.call({
			method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.get_linked_buyers",
			args: { purchase_invoice: frm.doc.name },
			callback(r) {
				const names = r.message || [];
				if (names.length === 1) {
					ask(names[0]);
				}
			},
		});
	};

	if (po && frappe.meta.has_field("Purchase Order", "purchase_efactura")) {
		frappe.db.get_value("Purchase Order", po, "purchase_efactura").then((r) => {
			const name = r.message && r.message.purchase_efactura;
			if (name) {
				ask(name);
				return;
			}
			ask_from_buyers();
		});
		return;
	}

	ask_from_buyers();
}
