const pf_invoice_api = "erpnext_moldova_efactura.utils.pf_invoice.";

frappe.ui.form.on("Purchase Factura", {
	refresh(frm) {
		if (frm.is_new() && frm.doc.amended_from) {
			frm.doc.purchase_invoice = null;
			(frm.doc.items || []).forEach((row) => {
				row.purchase_invoice = null;
				row.pi_detail = null;
			});
			frm.refresh_field("purchase_invoice");
			frm.refresh_field("items");
		}
		pf_setup_new_supplier(frm);
		pf_setup_new_item(frm);
		pf_currency_labels(frm);
		if (frappe.model.can_create("Purchase Factura")) {
			frm.add_custom_button(__("Import Document"), erpnext_moldova_efactura.pf.import_pdf);
		}
		if (frm.doc.provider && frm.doc.docstatus === 0) {
			frm.set_intro(
				__(
					"Imported original values are preserved. Map the ERP items and check quantities and amounts before submission. Digital signature verification has not been performed."
				),
				"blue"
			);
		} else {
			frm.set_intro();
		}
		const originals = [
			"original_format",
			"original_file",
			"f_series",
			"f_number",
			"issue_date",
			"delivery_date",
			"f_supplier_idno",
			"f_supplier_name",
			"f_supplier_vat_id",
			"f_supplier_taxpayer_type",
			"f_supplier_address",
			"f_supplier_bank_account",
			"f_supplier_bank_name",
			"f_supplier_bank_code",
			"f_customer_idno",
			"f_customer_name",
			"f_customer_vat_id",
			"f_customer_taxpayer_type",
			"f_customer_address",
			"f_customer_bank_account",
			"f_customer_bank_name",
			"f_customer_bank_code",
			"f_currency",
			"f_provider_reference",
			"f_provider_account",
			"f_contract_reference",
			"f_related_document_type",
			"f_related_document_number",
			"f_related_document_date",
		];
		originals.forEach((field) => frm.set_df_property(field, "read_only", !!frm.doc.provider));
		[
			"supplier_item_code",
			"supplier_item_name",
			"supplier_uom",
			"f_qty",
			"f_rate",
			"f_net_amount",
			"f_vat_rate",
			"f_vat_amount",
		].forEach((field) =>
			frm.fields_dict.items.grid.update_docfield_property(
				field,
				"read_only",
				!!frm.doc.provider
			)
		);
		if (frm.is_new() || frm.doc.docstatus !== 0 || !frm.has_perm("write")) return;
		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(__("Open Purchase Invoice"), () =>
				frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice)
			);
			frm.add_custom_button(
				__("Unlink Purchase Invoice"),
				() =>
					frappe.confirm(
						__(
							"Remove the fiscal link? The Purchase Invoice and its accounting entries will be retained."
						),
						() => pf_action(frm, "unlink_purchase_invoice", { name: frm.doc.name })
					),
				__("Actions")
			);
		} else {
			if (frappe.model.can_create("Purchase Order")) {
				frm.add_custom_button(
					__("Purchase Order"),
					() => {
						if (frm.is_dirty()) return frappe.msgprint(__("Save the factura first"));
						frappe.model.open_mapped_doc({
							method: pf_invoice_api + "make_purchase_order",
							frm,
						});
					},
					__("Create")
				);
			}
			if (frappe.model.can_create("Purchase Invoice")) {
				frm.add_custom_button(
					__("Purchase Invoice"),
					() => {
						if (frm.is_dirty()) return frappe.msgprint(__("Save the factura first"));
						frappe.model.open_mapped_doc({
							method: pf_invoice_api + "make_purchase_invoice",
							frm,
						});
					},
					__("Create")
				);
			}
			frm.add_custom_button(
				__("Link Purchase Invoice"),
				() => {
					if (frm.is_dirty()) return frappe.msgprint(__("Save the factura first"));
					frappe.prompt(
						[
							{
								fieldname: "purchase_invoice",
								fieldtype: "Link",
								label: __("Purchase Invoice"),
								options: "Purchase Invoice",
								reqd: 1,
								get_query: () => ({
									filters: {
										company: frm.doc.company,
										supplier: frm.doc.supplier_party,
										docstatus: ["<", 2],
										is_return: 0,
									},
								}),
							},
						],
						(values) =>
							pf_action(frm, "link_purchase_invoice", {
								name: frm.doc.name,
								...values,
							}),
						__("Link Purchase Invoice")
					);
				},
				__("Actions")
			);
		}
	},
	currency: pf_currency_changed,
	f_currency: pf_currency_changed,
	issue_date: pf_currency_changed,
	f_conversion_rate: pf_preview,
	company: pf_party_defaults,
	supplier_party: pf_party_defaults,
});

function pf_supplier_title(name) {
	// Use the same legal-name normalization as Purchase eFactura.
	let text = String(name || "").replace(/["'«»„“”‘’`]/g, "");
	text = text.replace(/^\s*(?:S\s*\.\s*C\s*\.?|SC\b|I\s*\.\s*C\s*\.\s*S\s*\.?|ICS\b)\s*/i, "");
	const hadSrl = /\bS\s*\.\s*R\s*\.\s*L\s*\.?/i.test(text) || /\bSRL\b/i.test(text);
	const hadSa = /\bS\s*\.\s*A\s*\.?/i.test(text) || /\bSA\b/i.test(text);
	text = text
		.replace(/\bS\s*\.\s*R\s*\.\s*L\s*\.?/gi, " ")
		.replace(/\bSRL\b/gi, " ")
		.replace(/\bS\s*\.\s*A\s*\.?/gi, " ")
		.replace(/\bSA\b/gi, " ")
		.replace(/\s+/g, " ")
		.trim()
		.toUpperCase();
	return [text, hadSrl ? "SRL" : "", hadSa ? "SA" : ""].filter(Boolean).join(" ");
}

function pf_setup_new_supplier(frm) {
	const field = frm.fields_dict.supplier_party;
	if (!field || field._pf_new_supplier_wrapped) return;
	field._pf_new_supplier_wrapped = true;
	field.df.get_route_options_for_new_doc = () => {
		const settings = frm._pf_supplier_settings || {};
		const opts = {};
		const title = pf_supplier_title(frm.doc.f_supplier_name);
		if (title) Object.assign(opts, { name_field: title, f_supplier_name: title });
		const idno = String(frm.doc.f_supplier_idno || "").trim();
		if (idno) opts[settings.supplier_idno_field || "tax_id"] = idno;
		if (settings.fiscal_territory) opts.territory = settings.fiscal_territory;
		return opts;
	};
	const original_new_doc = field.new_doc.bind(field);
	field.new_doc = async function () {
		// Resolve the configured IDNO field before Quick Entry consumes the defaults.
		const response = await frappe.call({
			method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura_settings.efactura_settings.get_form_settings",
		});
		frm._pf_supplier_settings = response.message || {};
		const original_get_label = this.get_label_value;
		const title = pf_supplier_title(frm.doc.f_supplier_name);
		if (title) this.get_label_value = () => title;
		try {
			return original_new_doc();
		} finally {
			this.get_label_value = original_get_label;
		}
	};
}

function pf_action(frm, action, args) {
	frappe.call({
		method: pf_invoice_api + action,
		args,
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

async function pf_party_defaults(frm) {
	const r = await frappe.call({
		method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura.party_defaults",
		args: { company: frm.doc.company, supplier_party: frm.doc.supplier_party },
	});
	if (!r.message) return;
	const values = { ...r.message };
	if (frm.doc.provider) {
		delete values.f_supplier_idno;
		delete values.f_customer_idno;
	}
	await frm.set_value(values);
}

function pf_currency_labels(frm) {
	frm.set_currency_labels(["net_total", "vat_total", "total"], frm.doc.currency);
	frm.set_currency_labels(["f_net_total", "f_vat_total", "f_total"], frm.doc.f_currency);
	frm.set_currency_labels(
		["rate", "rate_with_vat", "amount", "net_amount", "vat_amount"],
		frm.doc.currency,
		"items"
	);
	frm.set_currency_labels(
		["f_rate", "f_rate_with_vat", "f_amount", "f_net_amount", "f_vat_amount"],
		frm.doc.f_currency,
		"items"
	);
	frm.set_df_property(
		"f_conversion_rate",
		"read_only",
		frm.doc.docstatus !== 0 || frm.doc.currency === frm.doc.f_currency
	);
}

function pf_currency_changed(frm) {
	if (frm._pf_preview_running) return;
	frm.doc.f_conversion_rate = 0;
	return pf_preview(frm);
}

async function pf_preview(frm) {
	if (frm._pf_preview_running || frm.doc.docstatus !== 0) return;
	pf_currency_labels(frm);
	if (!frm.doc.company || !(frm.doc.items || []).length) return;
	if (frm.doc.items.some((r) => !flt(r.f_qty))) return;
	const request = (frm._pf_preview_request || 0) + 1;
	frm._pf_preview_request = request;
	const response = await frappe.call({
		method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura.preview_amounts",
		args: { document: frm.doc },
	});
	if (request !== frm._pf_preview_request || !response.message) return;
	frm._pf_preview_running = true;
	try {
		await frm.set_value(response.message.header);
		const fields = [
			"item_name",
			"f_uom",
			"uom",
			"f_conversion_factor",
			"conversion_factor",
			"stock_uom",
			"stock_qty",
			"qty",
			"f_rate_with_vat",
			"f_net_amount",
			"f_vat_amount",
			"f_amount",
			"rate",
			"rate_with_vat",
			"net_amount",
			"vat_amount",
			"amount",
		];
		for (const row of response.message.items) {
			const local = (frm.doc.items || []).find((r) => r.name === row.name);
			if (local)
				fields.forEach((key) => {
					local[key] = row[key];
				});
		}
		frm.refresh_field("items");
		pf_currency_labels(frm);
	} finally {
		frm._pf_preview_running = false;
	}
}

function pf_original_amount_changed(frm, cdt, cdn) {
	if (frm.doc.provider) return;
	const row = locals[cdt][cdn];
	row.f_net_amount = flt(flt(row.f_qty) * flt(row.f_rate), 2);
	row.f_vat_amount = flt((row.f_net_amount * flt(row.f_vat_rate)) / 100, 2);
	return pf_preview(frm);
}

frappe.ui.form.on("Purchase Factura Item", {
	form_render(frm, cdt, cdn) {
		const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
		const grid_row = grid && grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
		const control =
			(grid_row && grid_row.grid_form && grid_row.grid_form.fields_dict.item_code) ||
			(grid_row && grid_row.get_field && grid_row.get_field("item_code"));
		pf_wrap_item_new_doc(control, () => pf_item_title(locals[cdt][cdn]));
	},
	f_qty: pf_original_amount_changed,
	f_rate: pf_original_amount_changed,
	f_vat_rate: pf_original_amount_changed,
	f_net_amount: pf_preview,
	f_vat_amount: pf_preview,
	item_code: pf_preview,
	supplier_uom: pf_preview,
	f_uom: pf_preview,
	uom: pf_preview,
});

function pf_item_title(row) {
	return ((row && (row.supplier_item_name || row.supplier_item_code)) || "").trim();
}

function pf_item_route_options(title) {
	if (!title) return {};
	return {
		name_field: title,
		item_code: title,
		item_name: title,
	};
}

function pf_wrap_item_new_doc(control, get_title) {
	if (!control || typeof control.new_doc !== "function" || control._pf_item_new_doc_wrapped) {
		return;
	}
	control._pf_item_new_doc_wrapped = true;
	const original_new_doc = control.new_doc.bind(control);
	control.new_doc = function () {
		const title = (get_title(this) || "").trim();
		const original_get_label = this.get_label_value.bind(this);
		if (title) this.get_label_value = () => title;
		try {
			const result = original_new_doc();
			if (title && frappe.route_options) {
				frappe.route_options.name_field = title;
				frappe.route_options.item_code = title;
				frappe.route_options.item_name = title;
			}
			return result;
		} finally {
			this.get_label_value = original_get_label;
		}
	};
}

function pf_setup_new_item(frm) {
	const df = frm.get_docfield("items", "item_code");
	if (df) {
		df.get_route_options_for_new_doc = (link) =>
			pf_item_route_options(pf_item_title((link && link.doc) || {}));
	}
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (grid && !grid._pf_new_item_refresh_wrapped && typeof grid.refresh === "function") {
		grid._pf_new_item_refresh_wrapped = true;
		const original_refresh = grid.refresh.bind(grid);
		grid.refresh = function (...args) {
			const result = original_refresh(...args);
			setTimeout(() => pf_wrap_item_grid_controls(frm), 0);
			return result;
		};
	}
	pf_wrap_item_grid_controls(frm);
}

function pf_wrap_item_grid_controls(frm) {
	const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
	if (!grid) return;
	(grid.grid_rows || []).forEach((row) => {
		pf_patch_row_make_control(row);
		const control =
			(row.on_grid_fields_dict && row.on_grid_fields_dict.item_code) ||
			(row.grid_form && row.grid_form.fields_dict && row.grid_form.fields_dict.item_code);
		pf_wrap_item_new_doc(control, (link) => pf_item_title((link && link.doc) || row.doc));
	});
}

function pf_patch_row_make_control(row) {
	if (!row || row._pf_make_control_patched || typeof row.make_control !== "function") return;
	row._pf_make_control_patched = true;
	const original_make_control = row.make_control.bind(row);
	row.make_control = function (column) {
		original_make_control(column);
		if (column && column.df && column.df.fieldname === "item_code" && column.field) {
			pf_wrap_item_new_doc(column.field, (link) =>
				pf_item_title((link && link.doc) || row.doc)
			);
		}
	};
}
