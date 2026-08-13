frappe.ui.form.on("eFactura Buyer", {
	onload(frm) {
		if (frm.is_new()) {
			frappe.show_alert({
				message: __("Incoming e-Factura cannot be created manually. Use Fetch from e-Factura."),
				indicator: "orange",
			});
			frappe.set_route("List", "eFactura Buyer");
		}
	},
	refresh(frm) {
		lock_items_grid(frm);

		if (frm.is_new()) {
			return;
		}

		const efActions = __("eFactura Actions");

		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Fetch Details"), () => {
				frappe.call({
					method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.fetch_details",
					args: { name: frm.doc.name },
					freeze: true,
					callback(r) {
						if (!r.exc) {
							frm.reload_doc();
						}
					},
				});
			});

			frm.add_custom_button(__("Map Items"), () => open_map_dialog(frm));
		}

		if (frm.doc.ef_series && frm.doc.ef_number) {
			frm.add_custom_button(
				__("Download XML"),
				() => {
					const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.download_xml?name=${encodeURIComponent(frm.doc.name)}`;
					window.open(frappe.urllib.get_full_url(endpoint), "_blank");
				},
				efActions
			);
		}

		if (frm.doc.docstatus === 1 && cint(frm.doc.ef_status) !== -1 && frm.doc.ef_series && frm.doc.ef_number) {
			frm.add_custom_button(
				__("Download PDF"),
				() => {
					const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.download_pdf?name=${encodeURIComponent(frm.doc.name)}`;
					window.open(frappe.urllib.get_full_url(endpoint), "_blank");
				},
				efActions
			);
		}

		if (frm.doc.ef_series && frm.doc.ef_number) {
			frm.add_custom_button(
				__("Update Status"),
				() => {
					frappe.call({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.update_status",
						args: { name: frm.doc.name },
						freeze: true,
						freeze_message: __("Updating e-Factura status..."),
						callback(r) {
							if (!r.exc) {
								frappe.show_alert({
									message: __("e-Factura status updated successfully."),
									indicator: "green",
								});
								frm.reload_doc();
							}
						},
					});
				},
				efActions
			);
		}

		if (frm.doc.docstatus === 1) {
			const efStatus = cint(frm.doc.ef_status);
			if ([1, 7, 9].includes(efStatus)) {
				frm.add_custom_button(
					__("Accept"),
					() => {
						frappe.call({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.accept_invoice",
							args: { name: frm.doc.name },
							freeze: true,
							callback() {
								frm.reload_doc();
							},
						});
					},
					efActions
				);
			}
			if ([1, 7, 9, 3].includes(efStatus)) {
				frm.add_custom_button(
					__("Sign"),
					() => sign_buyer_xml_moldsign(frm),
					efActions
				);
			}

			if (frm.doc.supplier && (frm.doc.items || []).length && !frm.doc.purchase_invoice) {
				frm.add_custom_button(
					__("Create Purchase Invoice"),
					() => {
						frappe.model.open_mapped_doc({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.make_purchase_invoice",
							frm: frm,
						});
					},
					__("Purchase Invoice")
				);

				frm.add_custom_button(
					__("Link Purchase Invoice"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "purchase_invoice",
									fieldtype: "Link",
									options: "Purchase Invoice",
									label: __("Purchase Invoice"),
									reqd: 1,
									get_query: () => ({
										filters: frm.doc.supplier ? { supplier: frm.doc.supplier } : {},
									}),
								},
							],
							(values) => {
								frappe.call({
									method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.link_purchase_invoice",
									args: {
										name: frm.doc.name,
										purchase_invoice: values.purchase_invoice,
									},
									callback() {
										frm.reload_doc();
									},
								});
							},
							__("Link Purchase Invoice"),
							__("Link")
						);
					},
					__("Purchase Invoice")
				);
			}
		}

		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(
				__("Open Purchase Invoice"),
				() => frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice),
				__("Purchase Invoice")
			);
		}
	},
});

frappe.ui.form.on("eFactura Buyer Item", {
	item_code(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn);
	},
	ef_uom(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn);
	},
	uom(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn);
	},
	ef_qty(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn);
	},
});

function recalc_buyer_item_qtys(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row || frm.doc.docstatus !== 0) {
		return;
	}

	frappe.call({
		method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.get_item_qty_fields",
		args: {
			item_code: row.item_code,
			ef_uom: row.ef_uom,
			ef_qty: row.ef_qty,
			uom: row.uom,
		},
		callback(r) {
			if (r.exc || !r.message) {
				return;
			}
			const data = r.message;
			frappe.model.set_value(cdt, cdn, {
				stock_uom: data.stock_uom || null,
				stock_qty: flt(data.stock_qty),
				qty: flt(data.qty),
			});
		},
	});
}

function lock_items_grid(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) {
		return;
	}
	grid.cannot_add_rows = true;
	grid.cannot_delete_rows = true;
	grid.only_sortable = false;
	frm.set_df_property("items", "cannot_add_rows", 1);
	frm.set_df_property("items", "cannot_delete_rows", 1);
	if (grid.df) {
		grid.df.cannot_delete_rows = 1;
	}

	if (!grid._ef_buyer_checkbox_hooked) {
		grid._ef_buyer_checkbox_hooked = true;
		const original_refresh = grid.refresh.bind(grid);
		grid.refresh = function (...args) {
			const result = original_refresh(...args);
			hide_items_checkboxes(grid);
			return result;
		};
	}

	// Draft: map item + eFactura UOM (if auto-match missed) + PI UOM
	const editable = new Set(["item_code", "ef_uom", "uom"]);
	(grid.grid_rows || []).forEach((row) => {
		if (!row || !row.docfields) {
			return;
		}
		row.docfields.forEach((df) => {
			if (frm.doc.docstatus !== 0) {
				df.read_only = 1;
			} else {
				df.read_only = editable.has(df.fieldname) ? 0 : 1;
			}
		});
	});
	grid.refresh();
}

function hide_items_checkboxes(grid) {
	if (!grid || !grid.wrapper) {
		return;
	}
	grid.wrapper.find(".row-check").addClass("hidden").hide();
	grid.wrapper.find(".grid-row-check").closest("div").hide();
}

function open_map_dialog(frm) {
	if (frm.doc.docstatus !== 0) {
		frappe.msgprint(__("Item mapping is only allowed before submit."));
		return;
	}
	const rows = (frm.doc.items || []).filter((r) => !r.item_code);
	if (!rows.length) {
		frappe.msgprint(__("All items are already mapped."));
		return;
	}
	if (!frm.doc.supplier) {
		frappe.msgprint(__("Set Supplier first."));
		return;
	}

	const fields = rows.map((r) => ({
		fieldname: `item_${r.idx}`,
		fieldtype: "Link",
		options: "Item",
		label: `${r.idx}. ${r.supplier_item_code || ""} ${r.supplier_item_name || ""}`.trim(),
		reqd: 1,
	}));

	frappe.prompt(
		fields,
		(values) => {
			const mappings = rows.map((r) => ({
				idx: r.idx,
				item_code: values[`item_${r.idx}`],
			}));
			frappe.call({
				method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.save_item_mappings",
				args: { name: frm.doc.name, mappings },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		},
		__("Map Supplier Items"),
		__("Save")
	);
}

// -----------------------------
// Buyer XML Signing (MoldSign)
// -----------------------------
const MOLDSIGN_BASE = "http://localhost:8999";

async function ms_fetch(urlOrPath, options = {}) {
	const url = urlOrPath.startsWith("http") ? urlOrPath : `${MOLDSIGN_BASE}${urlOrPath}`;
	const resp = await fetch(url, {
		method: options.method || "GET",
		headers: options.headers || {},
		body: options.body,
		mode: "cors",
	});
	const text = await resp.text();
	const contentType = resp.headers.get("content-type") || "";
	let data = null;
	if (text && contentType.includes("application/json")) {
		try {
			data = JSON.parse(text);
		} catch (e) {}
	}
	return { resp, text, data };
}

async function ms_ping() {
	const { resp, text } = await ms_fetch("/certificates?private_only=true", {
		headers: { Accept: "application/json" },
	});
	if (!resp.ok) {
		throw new Error(`MoldSign not available: HTTP ${resp.status} ${text || ""}`.trim());
	}
}

async function ms_get_private_certs() {
	const { resp, data, text } = await ms_fetch("/certificates?private_only=true", {
		headers: { Accept: "application/json" },
	});
	if (!resp.ok) {
		throw new Error(`MoldSign certificates error: HTTP ${resp.status} ${text || ""}`.trim());
	}
	const list = data?.certificateModel || [];
	return list.filter((c) => c.privateKeyPresent);
}

async function ms_start_sign_session({ hash_base64, certificate }) {
	const payload = {
		algorithm: "SHA-1",
		signatureType: "Embedded",
		signFormat: "XAdES-T",
		contentType: "Text",
		data: hash_base64,
		certificate: certificate,
	};
	const { resp, text } = await ms_fetch("/sign/data", {
		method: "POST",
		headers: {
			Accept: "application/json",
			"Content-Type": "application/json",
		},
		body: JSON.stringify(payload),
	});
	if (resp.status !== 201) {
		throw new Error(`MoldSign start sign error: HTTP ${resp.status} ${text || ""}`.trim());
	}
	const location = resp.headers.get("location");
	if (!location) {
		throw new Error("MoldSign start sign error: Missing Location header.");
	}
	return location;
}

function sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function ms_poll_result(location, { timeout_ms = 120000, interval_ms = 800 } = {}) {
	const started = Date.now();
	while (true) {
		if (Date.now() - started > timeout_ms) {
			throw new Error("MoldSign signing timeout.");
		}
		const { resp, data, text } = await ms_fetch(location, {
			headers: { Accept: "application/json" },
		});
		if (resp.ok) {
			return { status: resp.status, data, text };
		}
		if (resp.status >= 400 && resp.status < 500) {
			const errHeader = resp.headers.get("error");
			throw new Error(`MoldSign signing failed: ${errHeader || text || `HTTP ${resp.status}`}`.trim());
		}
		await sleep(interval_ms);
	}
}

async function choose_certificate_dialog(certs) {
	const options = certs.map((c) => ({
		label: c.certificateName,
		value: c.certificateId,
	}));
	return new Promise((resolve, reject) => {
		const d = new frappe.ui.Dialog({
			title: __("Select certificate"),
			fields: [
				{
					fieldname: "cert",
					fieldtype: "Select",
					label: __("Certificate"),
					options: options,
					default: options[0]?.value || null,
					reqd: 1,
				},
			],
			primary_action_label: __("Sign"),
			primary_action: () => {
				const certId = d.get_value("cert");
				d.hide();
				resolve(certs.find((c) => c.certificateId === certId) || certs[0]);
			},
		});
		d.set_secondary_action(() => {
			d.hide();
			reject(new Error("Signing cancelled."));
		});
		d.show();
	});
}

async function sign_buyer_xml_moldsign(frm) {
	try {
		frappe.dom.freeze(__("Signing via MoldSign..."));
		await ms_ping();

		const r1 = await frappe.call({
			method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.get_xml_for_sign",
			args: { name: frm.doc.name },
		});
		const xml_base64 = r1.message?.xml_base64;
		const hash_base64 = r1.message?.hash_base64;
		if (!xml_base64 || !hash_base64) {
			throw new Error("Backend did not return XML for signing.");
		}

		const certs = await ms_get_private_certs();
		if (!certs.length) {
			throw new Error("No private certificates found in MoldSign.");
		}

		frappe.dom.unfreeze();
		const selected_cert = await choose_certificate_dialog(certs);
		frappe.dom.freeze(__("Signing via MoldSign..."));

		const location = await ms_start_sign_session({
			hash_base64: hash_base64,
			certificate: selected_cert,
		});
		const result = await ms_poll_result(location);
		await frappe.call({
			method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.process_signed_xml",
			args: {
				name: frm.doc.name,
				signature: result.data.base64File,
				content: xml_base64,
			},
		});
		frappe.show_alert({ message: __("Signed successfully"), indicator: "green" });
		frm.reload_doc();
	} catch (e) {
		frappe.msgprint({
			title: __("Signing error"),
			indicator: "red",
			message: e.message || String(e),
		});
	} finally {
		frappe.dom.unfreeze();
	}
}
