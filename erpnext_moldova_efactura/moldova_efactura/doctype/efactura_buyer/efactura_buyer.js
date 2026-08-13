frappe.ui.form.on("eFactura Buyer", {
	setup(frm) {
		ensure_supplier_idno_field(frm);
	},
	onload(frm) {
		if (frm.is_new()) {
			frappe.show_alert({
				message: __("Incoming e-Factura cannot be created manually. Use Fetch from e-Factura."),
				indicator: "orange",
			});
			frappe.set_route("List", "eFactura Buyer");
		}
	},
	supplier(frm) {
		persist_maps_when_supplier_set(frm);
	},
	refresh(frm) {
		lock_items_grid(frm);
		setup_new_supplier_from_factura(frm);
		ensure_supplier_idno_field(frm);

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
				frm.add_custom_button(
					__("Reject"),
					() => {
						frappe.prompt(
							[
								{
									fieldname: "reason",
									fieldtype: "Small Text",
									label: __("Rejection Reason"),
									reqd: 1,
									default: frm.doc.rejection_reason || "",
								},
							],
							(values) => {
								frappe.call({
									method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.reject_invoice",
									args: {
										name: frm.doc.name,
										reason: values.reason,
									},
									freeze: true,
									freeze_message: __("Rejecting e-Factura..."),
									callback() {
										frappe.show_alert({
											message: __("e-Factura rejected."),
											indicator: "orange",
										});
										frm.reload_doc();
									},
								});
							},
							__("Reject e-Factura"),
							__("Reject")
						);
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
		}

		if (frm.doc.supplier && (frm.doc.items || []).length && !frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Link Purchase Invoice"), () => link_purchase_invoice_dialog(frm));
		}

		if (frm.doc.docstatus === 1 && frm.doc.supplier && (frm.doc.items || []).length && !frm.doc.purchase_invoice) {
			const createMenu = __("Create");
			if (!frm.doc.purchase_order) {
				frm.add_custom_button(
					__("Purchase Order"),
					() => {
						frappe.model.open_mapped_doc({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.make_purchase_order",
							frm: frm,
						});
					},
					createMenu
				);
			}

			frm.add_custom_button(
				__("Purchase Invoice"),
				() => {
					frappe.model.open_mapped_doc({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_buyer.efactura_buyer.make_purchase_invoice",
						frm: frm,
					});
				},
				createMenu
			);
		}

		if (frm.doc.purchase_order) {
			frm.add_custom_button(__("Open Purchase Order"), () =>
				frappe.set_route("Form", "Purchase Order", frm.doc.purchase_order)
			);
		}

		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Open Purchase Invoice"), () =>
				frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice)
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

function normalize_supplier_title(name) {
	if (!name) {
		return "";
	}
	let text = String(name).replace(/["'«»„“”‘’`]/g, "");
	text = text.replace(/^\s*(?:S\s*\.\s*C\s*\.?|SC\b)\s*/i, "");
	const hadSrl = /\bS\s*\.\s*R\s*\.\s*L\s*\.?/i.test(text) || /\bSRL\b/i.test(text);
	const hadSa = /\bS\s*\.\s*A\s*\.?/i.test(text) || /\bSA\b/i.test(text);
	text = text
		.replace(/\bS\s*\.\s*R\s*\.\s*L\s*\.?/gi, " ")
		.replace(/\bSRL\b/gi, " ")
		.replace(/\bS\s*\.\s*A\s*\.?/gi, " ")
		.replace(/\bSA\b/gi, " ");
	text = text.replace(/\s+/g, " ").trim().toUpperCase();
	const suffixes = [];
	if (hadSrl) {
		suffixes.push("SRL");
	}
	if (hadSa) {
		suffixes.push("SA");
	}
	if (suffixes.length) {
		return [text, ...suffixes].filter(Boolean).join(" ");
	}
	return text;
}

function persist_maps_when_supplier_set(frm) {
	if (frm.doc.docstatus !== 0 || frm.is_new() || !frm.doc.supplier) {
		return;
	}
	if (!(frm.doc.items || []).some((r) => r.item_code)) {
		return;
	}
	if (frm._ef_persisting_maps) {
		return;
	}
	frm._ef_persisting_maps = true;
	frm.save(
		undefined,
		() => {
			frm._ef_persisting_maps = false;
		},
		null,
		() => {
			frm._ef_persisting_maps = false;
		}
	);
}

function ensure_supplier_idno_field(frm) {
	if (frm._supplier_idno_field) {
		return;
	}
	frappe.db.get_single_value("eFactura Settings", "supplier_idno_field").then((field) => {
		frm._supplier_idno_field = field;
	});
}

function setup_new_supplier_from_factura(frm) {
	const field = frm.fields_dict.supplier;
	const df = frm.get_docfield("supplier");
	if (!field || !df) {
		return;
	}

	df.get_route_options_for_new_doc = () => {
		const opts = {};
		const supplierName = normalize_supplier_title(frm.doc.ef_supplier_name);
		if (supplierName) {
			// Supplier.supplier_name is no_copy; title is set via name_field in new_doc wrap.
			opts.name_field = supplierName;
			opts.supplier_name = supplierName;
		}
		const idnoField = frm._supplier_idno_field;
		const idno = (frm.doc.ef_supplier_idno || "").trim();
		if (idnoField && idno) {
			opts[idnoField] = idno;
		}
		return opts;
	};

	if (field._ef_new_doc_wrapped) {
		return;
	}
	field._ef_new_doc_wrapped = true;
	const original_new_doc = field.new_doc.bind(field);
	field.new_doc = function () {
		const result = original_new_doc();
		// Link.new_doc overwrites name_field with the typed search text (often empty).
		const supplierName = normalize_supplier_title(frm.doc.ef_supplier_name);
		if (supplierName && frappe.route_options) {
			frappe.route_options.name_field = supplierName;
		}
		return result;
	};
}

function link_purchase_invoice_dialog(frm) {
	frappe.prompt(
		[
			{
				fieldname: "purchase_invoice",
				fieldtype: "Link",
				options: "Purchase Invoice",
				label: __("Purchase Invoice"),
				reqd: 1,
				get_query: () => ({
					filters: {
						docstatus: ["<", 2],
						...(frm.doc.supplier ? { supplier: frm.doc.supplier } : {}),
						...(frm.doc.company ? { company: frm.doc.company } : {}),
					},
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
				freeze: true,
				freeze_message: __("Matching Purchase Invoice..."),
				callback(r) {
					if (!r.exc) {
						frappe.show_alert({
							message: __("Purchase Invoice linked. Items mapped from the invoice."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
		__("Link Purchase Invoice"),
		__("Link")
	);
}

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
			const updates = {
				stock_uom: data.stock_uom || null,
				stock_qty: flt(data.stock_qty),
				qty: flt(data.qty),
			};
			if (!row.ef_uom && data.ef_uom) {
				updates.ef_uom = data.ef_uom;
			}
			if (!row.uom && data.uom) {
				updates.uom = data.uom;
			}
			frappe.model.set_value(cdt, cdn, updates);
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
