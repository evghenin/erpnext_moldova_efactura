const pf_invoice_api = "erpnext_moldova_efactura.utils.pf_invoice.";

frappe.ui.form.on("Purchase Factura", {
	setup(frm) {
		frm.set_query("expense_account", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
		frm.set_query("cost_center", "items", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},
	refresh(frm) {
		if (frappe.model.can_create("Purchase Factura")) {
			frm.add_custom_button(__("Import PDF"), erpnext_moldova_efactura.pf.import_pdf);
		}
		if (frm.doc.provider) {
			frm.set_intro(
				__(
					"Imported original values are preserved. Map the ERP items, check quantities and amounts, then mark the document reviewed. Digital signature verification has not been performed."
				),
				"blue"
			);
		}
		const originals = [
			"original_format",
			"original_file",
			"series",
			"number",
			"issue_date",
			"delivery_date",
			"supplier_idno",
			"supplier_name",
			"supplier_vat_id",
			"buyer_idno",
			"buyer_vat_id",
			"currency",
			"provider_reference",
			"provider_account",
			"contract_reference",
			"related_document_type",
			"related_document_number",
			"related_document_date",
		];
		originals.forEach((field) => frm.set_df_property(field, "read_only", !!frm.doc.provider));
		[
			"description",
			"source_uom",
			"source_qty",
			"source_rate",
			"net_amount",
			"vat_rate",
			"vat_amount",
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
										supplier: frm.doc.supplier,
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
	company: pf_party_defaults,
	supplier: pf_party_defaults,
});

function pf_action(frm, action, args) {
	frappe.call({
		method: pf_invoice_api + action,
		args,
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function pf_party_defaults(frm) {
	if (frm.doc.provider) return;
	frappe.call({
		method: "erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura.party_defaults",
		args: { company: frm.doc.company, supplier: frm.doc.supplier },
		callback: (r) => r.message && frm.set_value(r.message),
	});
}

function pf_recalculate(frm, cdt, cdn) {
	if (frm.doc.provider) return;
	const row = locals[cdt][cdn];
	const net = flt(flt(row.source_qty) * flt(row.source_rate), 2);
	const vat = flt((net * flt(row.vat_rate)) / 100, 2);
	frappe.model.set_value(cdt, cdn, { net_amount: net, vat_amount: vat, amount: net + vat });
	frm.set_value("reviewed", 0);
}

frappe.ui.form.on("Purchase Factura Item", {
	source_qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!frm.doc.provider) frappe.model.set_value(cdt, cdn, "qty", row.source_qty);
		pf_recalculate(frm, cdt, cdn);
	},
	source_rate: pf_recalculate,
	vat_rate: pf_recalculate,
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.item_code && !row.uom) {
			frappe.db.get_value("Item", row.item_code, "stock_uom").then((r) => {
				if (r.message?.stock_uom)
					frappe.model.set_value(cdt, cdn, "uom", r.message.stock_uom);
			});
		}
		frm.set_value("reviewed", 0);
	},
	uom(frm) {
		frm.set_value("reviewed", 0);
	},
	qty(frm) {
		frm.set_value("reviewed", 0);
	},
});
