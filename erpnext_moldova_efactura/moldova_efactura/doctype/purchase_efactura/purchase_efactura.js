frappe.ui.form.on("Purchase eFactura", {
	setup(frm) {
		ensure_supplier_idno_field(frm);
	},
	onload(frm) {
		if (frm.is_new()) {
			frappe.show_alert({
				message: __("Purchase eFactura cannot be created manually. Use Fetch from e-Factura."),
				indicator: "orange",
			});
			frappe.set_route("List", "Purchase eFactura");
		}
	},
	supplier(frm) {
		set_document_currency_from_supplier(frm).then(() => {
			return validate_supplier_idno(frm);
		}).then((ok) => {
			if (ok) {
				persist_maps_when_supplier_set(frm);
			}
		});
	},
	company(frm) {
		if (frm.doc.docstatus !== 0 || frm.doc.supplier || !frm.doc.company) {
			return;
		}
		frappe.db.get_value("Company", frm.doc.company, "default_currency").then((r) => {
			const cur = r.message && r.message.default_currency;
			if (cur) {
				frm.set_value("currency", cur);
			}
		});
	},
	currency(frm) {
		apply_currency_rules(frm).then(() => apply_pef_document_amounts(frm));
		ef_set_items_grid_currency_labels(frm);
	},
	issue_date(frm) {
		apply_currency_rules(frm).then(() => apply_pef_document_amounts(frm));
	},
	ef_conversion_rate(frm) {
		apply_pef_document_amounts(frm);
	},
	before_save(frm) {
		return validate_supplier_idno(frm);
	},
	refresh(frm) {
		lock_items_grid(frm);
		setup_new_supplier_from_factura(frm);
		setup_new_item_from_factura(frm);
		ensure_supplier_idno_field(frm);
		autofill_ef_details(frm, "supplier");
		autofill_ef_details(frm, "customer");
		autofill_ef_details(frm, "transporter");
		frm.set_df_property("currency", "read_only", frm.doc.docstatus !== 0);
		apply_currency_rules(frm);
		ef_set_items_grid_currency_labels(frm);

		if (frm.is_new()) {
			return;
		}

		const canWrite = frm.has_perm("write");
		const efActions = __("eFactura Actions");

		if (frm.doc.docstatus === 0 && canWrite) {
			frm.add_custom_button(__("Map Items"), () => open_map_dialog(frm));
		}

		if ((frm.doc.items || []).length && frm.doc.docstatus !== 2 && canWrite) {
			frm.add_custom_button(__("Link Invoice"), () => link_purchase_invoice_dialog(frm));
		}

		if (frm.doc.ef_series && frm.doc.ef_number) {
			frm.add_custom_button(
				__("Download XML"),
				() => {
					const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.download_xml?name=${encodeURIComponent(frm.doc.name)}`;
					window.open(frappe.urllib.get_full_url(endpoint), "_blank");
				},
				efActions
			);
		}

		if (frm.doc.docstatus === 1 && cint(frm.doc.ef_status) !== -1 && frm.doc.ef_series && frm.doc.ef_number) {
			frm.add_custom_button(
				__("Download PDF"),
				() => {
					const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.download_pdf?name=${encodeURIComponent(frm.doc.name)}`;
					window.open(frappe.urllib.get_full_url(endpoint), "_blank");
				},
				efActions
			);
		}

		if (frm.doc.ef_series && frm.doc.ef_number && canWrite) {
			frm.add_custom_button(
				__("Update Status"),
				() => {
					frappe.call({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.update_status",
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

		if (frm.doc.docstatus === 1 && canWrite) {
			const efStatus = cint(frm.doc.ef_status);
			if ([1, 7, 9].includes(efStatus)) {
				frm.add_custom_button(
					__("Accept"),
					() => {
						frappe.call({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.accept_invoice",
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
									method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.reject_invoice",
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

		if (frm.doc.docstatus !== 2 && frm.doc.supplier && (frm.doc.items || []).length && canWrite) {
			const createMenu = __("Create");
			if (frappe.model.can_create("Purchase Order")) {
				frm.add_custom_button(
					__("Purchase Order"),
					() => {
						frappe.model.open_mapped_doc({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.make_purchase_order",
							frm: frm,
						});
					},
					createMenu
				);
			}

			if (
				frappe.model.can_create("Purchase Invoice") &&
				!(frm.doc.items || []).some((row) => row.purchase_invoice)
			) {
				frm.add_custom_button(
					__("Purchase Invoice"),
					() => {
						frappe.model.open_mapped_doc({
							method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.make_purchase_invoice",
							frm: frm,
						});
					},
					createMenu
				);
			}
		}
	},
});

frappe.ui.form.on("Purchase eFactura Item", {
	form_render(frm, cdt, cdn) {
		const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
		const grid_row = grid && grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
		const control =
			(grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.item_code) ||
			(grid_row && grid_row.get_field && grid_row.get_field("item_code"));
		wrap_item_link_new_doc(control, () => item_title_from_row(locals[cdt][cdn]));
	},
	item_code(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn, { refresh_uom: true, refresh_ef: true });
	},
	ef_uom(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn, { refresh_ef: true });
	},
	uom(frm, cdt, cdn) {
		recalc_buyer_item_qtys(frm, cdt, cdn, { refresh_uom: true });
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

function autofill_ef_details(frm, party_type) {
	const name = frm.doc[`ef_${party_type}_name`];
	const idno = frm.doc[`ef_${party_type}_idno`];
	let html = "<span></span>";

	if (name || idno) {
		const cell = (value) =>
			frappe.utils.escape_html(value ? String(value) : __("Unknown"));
		const row = (label, value, always) => {
			if (!always && !value) {
				return "";
			}
			return `<tr>
				<td width="40%"><b>${frappe.utils.escape_html(__(label))}:</b></td>
				<td>${cell(value)}</td>
			</tr>`;
		};
		const taxpayer_type = frm.doc[`ef_${party_type}_taxpayer_type`];
		html = '<table class="table">';
		html += row("Name", name, true);
		html += row("IDNO", idno, true);
		html += row("VAT ID", frm.doc[`ef_${party_type}_vat_id`], party_type !== "transporter");
		html += row("Address", frm.doc[`ef_${party_type}_address`], true);
		if (taxpayer_type) {
			html += row("Taxpayer Type", __(taxpayer_type, null, "Purchase eFactura"), true);
		}
		html += row("Bank account", frm.doc[`ef_${party_type}_bank_account`]);
		html += row("Bank name", frm.doc[`ef_${party_type}_bank_name`]);
		html += row("Bank code", frm.doc[`ef_${party_type}_bank_code`]);
		html += "</table>";
	}

	frm.set_df_property(`ef_${party_type}_details`, "options", html);
}

function set_document_currency_from_supplier(frm) {
	if (frm.doc.docstatus !== 0 || !frm.doc.supplier) {
		return Promise.resolve();
	}
	return frappe.db.get_value("Supplier", frm.doc.supplier, "default_currency").then((r) => {
		const cur = r.message && r.message.default_currency;
		if (cur && frm.doc.currency !== cur) {
			return frm.set_value("currency", cur);
		}
	});
}

async function apply_currency_rules(frm) {
	const cur = frm.doc.currency;
	const efCur = frm.doc.ef_currency;
	if (!cur || !efCur) {
		frm.set_df_property("ef_conversion_rate", "read_only", 0);
		return;
	}
	if (cur === efCur) {
		if (flt(frm.doc.ef_conversion_rate) !== 1) {
			await frm.set_value("ef_conversion_rate", 1);
		}
		frm.set_df_property("ef_conversion_rate", "read_only", 1);
		return;
	}
	frm.set_df_property("ef_conversion_rate", "read_only", frm.doc.docstatus !== 0);
	if (frm.doc.docstatus !== 0) {
		return;
	}
	const date = frm.doc.issue_date || frappe.datetime.get_today();
	try {
		const r = await frappe.call({
			method: "erpnext.setup.utils.get_exchange_rate",
			args: {
				from_currency: cur,
				to_currency: efCur,
				transaction_date: date,
			},
		});
		const rate = r && r.message ? flt(r.message) : 0;
		if (rate && rate > 0 && flt(frm.doc.ef_conversion_rate) !== rate) {
			await frm.set_value("ef_conversion_rate", rate);
		}
	} catch (e) {
		// leave editable for manual input
	}
}

function apply_pef_document_amounts(frm) {
	if (!flt(frm.doc.ef_total) && !(frm.doc.items || []).some((row) => flt(row.ef_amount))) {
		return;
	}
	const conv = flt(frm.doc.ef_conversion_rate) || 1;
	(frm.doc.items || []).forEach((row) => {
		row.rate = flt(row.ef_rate) / conv;
		row.rate_with_vat = flt(row.ef_rate_with_vat) / conv;
		row.amount = flt(row.ef_amount) / conv;
		row.net_amount = flt(row.ef_net_amount) / conv;
		row.vat_amount = flt(row.ef_vat_amount) / conv;
	});
	frm.refresh_field("items");
	frm.set_value("net_total", flt(frm.doc.ef_net_total) / conv);
	frm.set_value("vat_total", flt(frm.doc.ef_vat_total) / conv);
	frm.set_value("total", flt(frm.doc.ef_total) / conv);
}

function ef_set_items_grid_currency_labels(frm) {
	frm.set_currency_labels(
		["rate", "rate_with_vat", "amount", "vat_amount", "net_amount"],
		frm.doc.currency,
		"items"
	);
	frm.set_currency_labels(
		["ef_rate", "ef_rate_with_vat", "ef_amount", "ef_vat_amount", "ef_net_amount"],
		frm.doc.ef_currency,
		"items"
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

function normalize_idno(value) {
	return String(value || "").replace(/\D+/g, "");
}

function validate_supplier_idno(frm) {
	if (!frm.doc.supplier || !frm.doc.ef_supplier_idno) {
		return Promise.resolve(true);
	}
	const expected = normalize_idno(frm.doc.ef_supplier_idno);
	if (!expected) {
		return Promise.resolve(true);
	}
	const check = (field) => {
		if (!field) {
			return Promise.resolve(true);
		}
		return frappe.db.get_value("Supplier", frm.doc.supplier, field).then((r) => {
			const raw = (r.message && r.message[field]) || "";
			if (normalize_idno(raw) === expected) {
				return true;
			}
			frappe.throw(
				__("Supplier {0} IDNO ({1}) does not match e-Factura supplier IDNO {2}", [
					frm.doc.supplier,
					raw || __("not set"),
					frm.doc.ef_supplier_idno,
				])
			);
		});
	};
	if (frm._supplier_idno_field !== undefined) {
		return check(frm._supplier_idno_field);
	}
	return frappe.db.get_single_value("eFactura Settings", "supplier_idno_field").then((field) => {
		frm._supplier_idno_field = field;
		return check(field);
	});
}

function item_title_from_row(row) {
	return ((row && (row.supplier_item_name || row.supplier_item_code)) || "").trim();
}

function item_route_options_from_title(title) {
	if (!title) {
		return {};
	}
	return {
		name_field: title,
		item_code: title,
		item_name: title,
	};
}

function wrap_item_link_new_doc(control, getTitle) {
	if (!control || typeof control.new_doc !== "function" || control._ef_item_new_doc_wrapped) {
		return;
	}
	control._ef_item_new_doc_wrapped = true;
	const original_new_doc = control.new_doc.bind(control);
	control.new_doc = function () {
		const title = (getTitle(this) || "").trim();
		const origGetLabel = this.get_label_value.bind(this);
		if (title) {
			// Link.new_doc overwrites name_field with typed search (often empty).
			this.get_label_value = function () {
				return title;
			};
		}
		try {
			const result = original_new_doc();
			if (title && frappe.route_options) {
				frappe.route_options.name_field = title;
				frappe.route_options.item_code = title;
				frappe.route_options.item_name = title;
			}
			return result;
		} finally {
			this.get_label_value = origGetLabel;
		}
	};
}

function setup_new_item_from_factura(frm) {
	const df = frm.get_docfield("items", "item_code");
	if (df) {
		df.get_route_options_for_new_doc = (link) =>
			item_route_options_from_title(item_title_from_row((link && link.doc) || {}));
	}
	wrap_item_code_grid_controls(frm);
}

function wrap_item_code_grid_controls(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) {
		return;
	}
	(grid.grid_rows || []).forEach((row) => {
		const control =
			(row.on_grid_fields_dict && row.on_grid_fields_dict.item_code) ||
			(row.grid_form && row.grid_form.fields_dict && row.grid_form.fields_dict.item_code);
		wrap_item_link_new_doc(control, (link) =>
			item_title_from_row((link && link.doc) || row.doc)
		);
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
	if (!frm.doc.supplier) {
		frappe.msgprint({
			title: __("Select a Supplier"),
			indicator: "orange",
			message: __("Select a Supplier on the e-Factura first"),
		});
		return;
	}

	frappe.prompt(
		[
			{
				fieldname: "purchase_invoice",
				fieldtype: "Link",
				options: "Purchase Invoice",
				label: __("Purchase Invoice"),
				reqd: 1,
				get_query: () => ({
					query: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.linkable_purchase_invoices",
					filters: {
						company: frm.doc.company,
						supplier: frm.doc.supplier,
					},
				}),
			},
		],
		(values) => {
			frappe.call({
				method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.link_purchase_invoice",
				args: {
					name: frm.doc.name,
					purchase_invoice: values.purchase_invoice,
				},
				freeze: true,
				freeze_message: __("Matching Purchase Invoice..."),
				callback(r) {
					if (!r.exc) {
						frappe.show_alert({
							message: __("Purchase Invoice linked."),
							indicator: "green",
						});
						frm.reload_doc();
					}
				},
			});
		},
		__("Link Invoice"),
		__("Link")
	);
}

function recalc_buyer_item_qtys(frm, cdt, cdn, opts) {
	const row = locals[cdt][cdn];
	if (!row || frm.doc.docstatus !== 0) {
		return;
	}
	opts = opts || {};

	frappe.call({
		method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.get_item_qty_fields",
		args: {
			item_code: row.item_code,
			ef_uom: row.ef_uom,
			ef_qty: row.ef_qty,
			uom: row.uom,
			conversion_factor: opts.refresh_uom ? 0 : row.conversion_factor,
			ef_conversion_factor: opts.refresh_ef ? 0 : row.ef_conversion_factor,
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
				conversion_factor: flt(data.conversion_factor) || 1,
				ef_conversion_factor: flt(data.ef_conversion_factor) || 1,
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
			wrap_item_code_grid_controls(frm);
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
		get_route_options_for_new_doc: () => item_route_options_from_title(item_title_from_row(r)),
	}));

	const d = frappe.prompt(
		fields,
		(values) => {
			const mappings = rows.map((r) => ({
				idx: r.idx,
				item_code: values[`item_${r.idx}`],
			}));
			frappe.call({
				method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_efactura.purchase_efactura.save_item_mappings",
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
	rows.forEach((r) => {
		wrap_item_link_new_doc(d.fields_dict[`item_${r.idx}`], () => item_title_from_row(r));
	});
}

async function sign_buyer_xml_moldsign(frm) {
	try {
		await erpnext_moldova_efactura.moldsign.sign_purchase_efactura(frm.doc.name);
		frm.reload_doc();
	} catch (e) {
		frappe.msgprint({
			title: __("Signing error"),
			indicator: "red",
			message: e.message || String(e),
		});
	}
}
