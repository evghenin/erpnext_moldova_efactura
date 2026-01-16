// Copyright (c) 2025, Evgheni Nemerenco and contributors
// For license information, please see license.txt

frappe.ui.form.on('eFactura', {
    setup: function (frm) {
        frm.set_indicator_formatter("item_code", function (doc) {
            return doc.docstatus == 1 || doc.stock_qty <= doc.available_stock_qty ? "green" : "red";
        });
    },

    refresh(frm) {
        setup_reference_name_query(frm);
        update_supplier_party(frm);
        update_supplier_bank_account(frm);
        update_transporter_party(frm);
        apply_currency_rules(frm);
        ef_set_items_grid_currency_labels(frm);
        autofillEfDetails(frm, "supplier");
        autofillEfDetails(frm, "customer");
        autofillEfDetails(frm, "transporter");

        console.log('!');

        if (
			// !frm.doc.is_return &&
			frm.is_new() &&
			frm.has_perm("write") &&
			frappe.model.can_read("Delivery Note") &&
			frm.doc.docstatus === 0
		) {
			frm.add_custom_button(
				__("Delivery Note"),
				function () {
					if (!frm.doc.customer_party_type === 'Customer' || !frm.doc.customer_party) {
						frappe.throw({
							title: __("Mandatory"),
							message: __("Please Select a Customer Party Type \"Customer\" and Customer Party first."),
						});
					}
					erpnext.utils.map_current_doc({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.make_efactura_from_delivery_note",
						// args: {
						// 	for_reserved_stock: 1,
						// },
						source_doctype: "Delivery Note",
						target: frm,
						setters: {
							customer: frm.doc.customer,
						},
						get_query_filters: {
							docstatus: 1,
							status: ["not in", ["Canceled"]],
							// per_delivered: ["<", 99.99],
							company: frm.doc.company,
							// project: frm.doc.reference_doctype  == "Sales Invoice" ? frm.doc.reference_name : undefined,
						},
						allow_child_item_selection: true,
						child_fieldname: "items",
						child_columns: ["item_code", "item_name", "qty", "rate"],
					});
				},
				__("Get Items From")
			);
		}

        if (!frm.is_new()) {
            frm.add_custom_button(
                __("Download XML"),
                function () {
                    const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.download_xml?efactura_name=${encodeURIComponent(frm.doc.name)}`;
                    const url = frappe.urllib.get_full_url(endpoint);
                    window.open(url, "_blank");
                },
                __("eFactura Actions")
            );
        }

        if (
            !frm.is_new() && 
            frm.doc.docstatus === 1 &&
            frm.doc.ef_status != -1
        ) {
            frm.add_custom_button(
                __("Download PDF"), 
                function () {
                    const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.download_pdf?efactura_name=${encodeURIComponent(frm.doc.name)}`;
                    const url = frappe.urllib.get_full_url(endpoint);
                    window.open(url, "_blank");
                },
                __("eFactura Actions")
            );
        }

        if (
            !frm.is_new() && 
            frm.doc.docstatus === 1 && 
            frm.doc.ef_status == -1
        ) {
            frm.add_custom_button(
                __("Update Dates"),
                function () {
                    const d = new frappe.ui.Dialog({
                        title: __("Update Dates"),
                        fields: [
                            {
                                fieldname: "issue_date",
                                fieldtype: "Date",
                                label: __("Issue Date"),
                                reqd: 1,
                                default: frappe.datetime.get_today(),
                            },
                            {
                                fieldname: "delivery_date",
                                fieldtype: "Date",
                                label: __("Delivery Date"),
                                reqd: 1,
                                default: frm.doc.delivery_date || frappe.datetime.get_today(),
                            },
                        ],
                        primary_action_label: __("Update"),
                        primary_action(values) {
                            frappe.call({
                                method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.update_dates",
                                args: {
                                    efactura_name: frm.doc.name,
                                    issue_date: values.issue_date,
                                    delivery_date: values.delivery_date,
                                },
                                freeze: true,
                                freeze_message: __("Updating dates..."),
                                callback: (r) => {
                                    const msg = r && r.message ? r.message : {};
                                    if (msg.issue_date) frm.set_value("issue_date", msg.issue_date);
                                    if (msg.delivery_date) frm.set_value("delivery_date", msg.delivery_date);

                                    frappe.show_alert(
                                        { message: __("Dates updated successfully."), indicator: "green" },
                                        5
                                    );
                                    d.hide();
                                },
                            });
                        },
                    });

                    // Initialize defaults each time, in case doc changes
                    d.set_values({
                        issue_date: frappe.datetime.get_today(),
                        delivery_date: frm.doc.delivery_date || frappe.datetime.get_today(),
                    });

                    d.show();
                },
                __("eFactura Actions")
            );

            frm.add_custom_button(
                __("Register Signed"), 
                async () => {
                    await sign_xml_moldsign(frm);
                },
                __("eFactura Actions")
            );

            frm.add_custom_button(
				__("Register Unsigned"),
                function () {
                    frappe.call({
                        method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.send_unsigned",
                        args: { efactura_name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Registering unsigned XML to e-Factura system..."),
                        callback: (r) => {
                            frappe.show_alert({
                                message: __("Unsigned XML registered successfully in e-Factura system."),
                                indicator: "green",
                            }, 5);
                            frm.reload_doc();
                        },
                        // error: (r) => {
                        //     frappe.show_alert({
                        //         title: __("Failed to register unsigned XML in e-Factura system."),
                        //         message: r,
                        //         indicator: "red",
                        //     }, 10);
                        // },
                    });
                },
                __("eFactura Actions")
            );
        }

        function autofillEfDetails(frm, party_type) {
            let html_content = '<span></span>';

            if (frm.doc[`ef_${party_type}_name`]) {
                html_content += '<table class="table">';

                html_content += `<tr>
                    <td><b>${__("Name")}:</b></td>
                    <td>${frm.doc[`ef_${party_type}_name`] || __("Unknown")}</td>
                </tr>`;

                html_content += `<tr>
                    <td width="40%"><b>${__("IDNO")}:</b></td>
                    <td>${frm.doc[`ef_${party_type}_idno`] || __("Unknown")}</td>
                </tr>`;

                html_content += `<tr>
                    <td><b>${__("VAT ID")}:</b></td>
                    <td>${frm.doc[`ef_${party_type}_vat_id`] || __("Unknown")}</td>
                </tr>`;

                html_content += `<tr>
                    <td><b>${__("Address")}:</b></td>
                    <td>${frm.doc[`ef_${party_type}_address`] || __("Unknown")}</td>
                </tr>`;

                if (frm.doc[`ef_${party_type}_bank_account`]) {
                    html_content += `<tr>
                        <td><b>${__("Bank account")}:</b></td>
                        <td>${frm.doc[`ef_${party_type}_bank_account`] || __("Unknown")}</td>
                    </tr>`;
                }

                if (frm.doc[`ef_${party_type}_bank_name`]) {
                    html_content += `<tr>
                        <td><b>${__("Bank name")}:</b></td>
                        <td>${frm.doc[`ef_${party_type}_bank_name`] || __("Unknown")}</td>
                    </tr>`;
                }

                if (frm.doc[`ef_${party_type}_bank_code`]) {
                    html_content += `<tr>
                        <td><b>${__("Bank code")}:</b></td>
                        <td>${frm.doc[`ef_${party_type}_bank_code`] || __("Unknown")}</td>
                    </tr>`;
                }

                const is_user = frm.doc[`ef_${party_type}_is_user`];
                const is_user_str =
                    is_user === "" ? __("Unknown") : __(is_user);

                html_content += `<tr>
                    <td><b>${__("Is eFactura User")}:</b></td>
                    <td>${is_user_str}</td>
                </tr>`;

                html_content += "</table>";
            }

            
            frm.set_df_property(`ef_${party_type}_details`, "options", html_content.replace('\'', '&#39;'));
        }
    },

    type: function(frm) {
        if (frm.doc.document_type == "Transfer") {
            frm.set_value("naming_series", "EF-.YYYY.-");
        } else if (frm.doc.document_type == "Non-Transfer") {
            frm.set_value("naming_series", "EF-NT-.YYYY.--");
        }
    },

    reference_doctype(frm) {
        frm.set_value('reference_name', null);
        setup_reference_name_query(frm);
        update_customer_party(frm);
        update_transporter_party(frm);
    },

    reference_name: function(frm) {
        update_customer_party(frm);
    },

    company(frm) {
        frm.set_value('reference_name', null);
        setup_reference_name_query(frm);

        // Keep supplier_party in sync when Supplier Party Type = Company
        update_supplier_party(frm);
        update_supplier_bank_account(frm);
        update_transporter_party(frm);
    },

    currency(frm) {
        apply_currency_rules(frm);
        ef_set_items_grid_currency_labels(frm);
    },

    ef_currency(frm) {
        apply_currency_rules(frm);
        ef_set_items_grid_currency_labels(frm);
    },

    issue_date(frm) {
        apply_currency_rules(frm);
    },

    supplier_party_type(frm) {
        // If type is unset: clear dependent fields
        if (!frm.doc.supplier_party_type) {
            frm.set_value('supplier_party', null);
            frm.set_value('supplier_bank_account', null);
        } else {
            // If switched to Company: set supplier_party from company (if available)
            update_supplier_party(frm);
        }

        // Bank account depends on supplier party selection
        if (!frm.doc.supplier_party) {
            frm.set_value('supplier_bank_account', null);
        }

        update_supplier_bank_account(frm);
    },

    supplier_party(frm) {
        // Clear bank account if party is unset
        if (!frm.doc.supplier_party) {
            frm.set_value('supplier_bank_account', null);
        }
        update_supplier_party(frm);
        update_supplier_bank_account(frm);
    },

    transporter_party_type(frm) {
        // If type is unset: clear dependent fields
        if (!frm.doc.transporter_party_type) {
            frm.set_value('transporter_party', null);
        } else {
            // If switched to Company: set transporter_party from company (if available)
            update_transporter_party(frm);
        }
    },

    transporter_party(frm) {
        update_transporter_party(frm);
    },

    items_add: async function(frm) {
        await ef_recalculate_totals(frm);
    },

    items_remove: async function(frm) {
        await ef_recalculate_totals(frm);
    },

    ef_conversion_rate: async function(frm) {
        await ef_recalculate_all_items_and_totals(frm);
    },
});

function setup_reference_name_query(frm) {
    frm.set_query('reference_name', function () {
        // If no doctype or company selected, show nothing
        if (!frm.doc.reference_doctype || !frm.doc.company) {
            return { filters: { name: ['=', ''] } };
        }

        return {
            filters: { company: frm.doc.company }
        };
    });
}

async function apply_currency_rules(frm) {
    const cur = frm.doc.currency;
    const efCur = frm.doc.ef_currency;

    // If currencies are missing, do not proceed
    if (!cur || !efCur) {
        frm.set_df_property('ef_conversion_rate', 'read_only', 0);
        return;
    }

    // Same currency: rate = 1 and read-only
    if (cur === efCur) {
        if (frm.doc.ef_conversion_rate !== 1) {
            frm.set_value('ef_conversion_rate', 1);
        }
        frm.set_df_property('ef_conversion_rate', 'read_only', 1);
        return;
    }

    // Different currencies: editable
    frm.set_df_property('ef_conversion_rate', 'read_only', 0);

    // Try to auto-fetch rate by issue_date (fallback to today)
    const date = frm.doc.issue_date || frappe.datetime.get_today();

    try {
        const r = await frappe.call({
            method: 'erpnext.setup.utils.get_exchange_rate',
            args: {
                from_currency: cur,
                to_currency: efCur,
                transaction_date: date
            }
        });

        const rate = r && r.message ? flt(r.message) : 0;
        if (rate && rate > 0) {
            frm.set_value('ef_conversion_rate', rate);
        }
    } catch (e) {
        // Leave editable for manual input
    }
}

/**
 * Supplier Party Type = Company rules:
 * - If supplier_party_type is Company and company is set, force supplier_party = company
 * - When company is set, supplier_party becomes read-only
 * - If company is cleared, supplier_party is NOT cleared and remains editable
 * - If supplier_party_type is not Company, supplier_party is editable (if type selected)
 */
function update_supplier_party(frm) {
    const isCompanySupplier = frm.doc.supplier_party_type === 'Company';
    const company = frm.doc.company;
    const hasType = !!frm.doc.supplier_party_type;

    // Disable supplier_party if supplier_party_type is not selected
    if (!hasType) {
        frm.set_df_property('supplier_party', 'read_only', 1);
    } else if (isCompanySupplier) {
        if (company) {
            if (frm.doc.supplier_party !== company) {
                frm.set_value('supplier_party', company);
            }
            frm.set_df_property('supplier_party', 'read_only', 1);
        } else {
            frm.set_df_property('supplier_party', 'read_only', 0);
        }
    } else {
        frm.set_df_property('supplier_party', 'read_only', 0);
    }
}

function update_supplier_bank_account(frm) {
    const hasType = !!frm.doc.supplier_party_type;
    const hasParty = !!frm.doc.supplier_party;

    // Disable supplier_bank_account if supplier_party_type or supplier_party is not selected
    frm.toggle_enable('supplier_bank_account', hasType && hasParty);

    if (hasParty) {
        set_default_supplier_bank_account(frm);
    }

    // Filter bank accounts based on party_type/party
    frm.set_query('supplier_bank_account', function() {
        if (!hasType || !hasParty) {
            return { filters: { name: ['=', ''] } };
        }

        // If supplier is a company, show only company bank accounts for that company
        if (frm.doc.supplier_party_type === 'Company') {
            return {
                filters: {
                    is_company_account: 1,
                    company: frm.doc.supplier_party
                }
            };
        }

        // Otherwise, show bank accounts linked to the selected party
        return {
            filters: {
                party_type: frm.doc.supplier_party_type,
                party: frm.doc.supplier_party
            }
        };
    });
}

async function set_default_supplier_bank_account(frm) {
    // Works only for Company supplier
    if (frm.doc.supplier_party_type !== 'Company') return;

    // Prefer supplier_party as the selected company; fallback to company field
    const company = frm.doc.supplier_party;
    if (!company) return;

    // If current bank account is already valid for this company, keep it
    if (frm.doc.supplier_bank_account) {
        try {
            const r = await frappe.call({
                method: 'frappe.client.get_value',
                args: {
                    doctype: 'Bank Account',
                    filters: { name: frm.doc.supplier_bank_account },
                    fieldname: ['company', 'is_company_account']
                }
            });

            const ba = r && r.message ? r.message : null;
            if (ba && cint(ba.is_company_account) === 1 && ba.company === company) {
                return; // already correct
            }
        } catch (e) {
            // Ignore and continue with default selection
        }
    }

    // 1) Try to get default company bank account
    try {
        const rDefault = await frappe.call({
            method: 'frappe.client.get_list',
            args: {
                doctype: 'Bank Account',
                fields: ['name'],
                filters: {
                    is_company_account: 1,
                    company: company,
                    is_default: 1
                },
                limit_page_length: 1
            }
        });

        const rowsDefault = (rDefault && rDefault.message) ? rDefault.message : [];
        if (rowsDefault.length) {
            await frm.set_value('supplier_bank_account', rowsDefault[0].name);
            return;
        }
    } catch (e) {
        // Ignore and try fallback
    }
}

function update_customer_party(frm) {
    const isSalesInvoiceRef = frm.doc.reference_doctype === 'Sales Invoice';
    const hasRefName = !!frm.doc.reference_name;

    if (isSalesInvoiceRef && hasRefName) {
        // Set customer_party read-only when reference is Sales Invoice with selected name
        frm.set_df_property('customer_party', 'read_only', 1);
    } else {
        // Set customer_party editable if reference is not Sales Invoice or no reference selected
        frm.set_df_property('customer_party', 'read_only', 0);
    }

    if (isSalesInvoiceRef && hasRefName) {
        // If reference is Sales Invoice, set customer_party from there
        update_customer_party_from_reference(frm);
    }
}

async function update_customer_party_from_reference(frm) {
    if (
        frm.doc.reference_doctype !== 'Sales Invoice' ||
        !frm.doc.reference_name
    ) {
        return;
    }

    try {
        const r = await frappe.call({
            method: 'frappe.client.get_value',
            args: {
                doctype: 'Sales Invoice',
                filters: { name: frm.doc.reference_name },
                fieldname: ['customer']
            }
        });

        if (r && r.message && r.message.customer) {
            if (frm.doc.customer_party !== r.message.customer) {
                await frm.set_value('customer_party', r.message.customer);
            }
        }
    } catch (e) {
        // fail silently
    }
}

/**
 * Transporter Party Type = Company rules:
 * - If transporter_party_type is Company and company is set, force transporter_party = company
 * - When company is set, transporter_party becomes read-only
 * - If company is cleared, transporter_party is NOT cleared and remains editable
 * - If transporter_party_type is not Company, transporter_party is editable (if type selected)
 */
function update_transporter_party(frm) {
    const isCompanyTransporter = frm.doc.transporter_party_type === 'Company';
    const company = frm.doc.company;
    const hasType = !!frm.doc.transporter_party_type;
    // const typeWasChanged = frm.doc.transporter_party_type !== frm.fields_dict['transporter_party_type'].last_value;

    // if (typeWasChanged) {
    //     // If type is unset: clear dependent fields
    //     frm.set_value('transporter_party', null);
    // }

    // Disable transporter_party if transporter_party_type is not selected
    if (!hasType) {
        frm.set_df_property('transporter_party', 'read_only', 1);
    } else if (isCompanyTransporter) {
        if (company) {
            if (frm.doc.transporter_party !== company) {
                frm.set_value('transporter_party', company);
            }
            frm.set_df_property('transporter_party', 'read_only', 1);
        } else {
            frm.set_df_property('transporter_party', 'read_only', 0);
        }
    } else {
        frm.set_df_property('transporter_party', 'read_only', 0);
    }

    frm.set_query('transporter_party', function () {
        // If no doctype or company selected, return empty filters
        if (!frm.doc.transporter_party_type) {
            return { filters: { } };
            // If transporter is Supplier, filter to suppliers only who is transporter 
        } else if (frm.doc.transporter_party_type === 'Supplier') {
            return {
                filters: { is_transporter: 1 }
            };
            // If transporter is Customer, filter to customer
        } else if (frm.doc.transporter_party_type === 'Customer'
                && frm.doc.customer_party_type === 'Customer' && frm.doc.customer_party) {
            return {
                filters: { name: frm.doc.customer_party }
            };
        } else if (frm.doc.transporter_party_type === 'Company' && frm.doc.company) {
            return {
                filters: { name: frm.doc.company }
            };
        }

        return {
            filters: { company: frm.doc.company }
        };
    });
}

frappe.ui.form.on('eFactura Item', {
    async item_code(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        await ef_item_apply_defaults_from_item(frm, row);
        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);
        frm.refresh_field('items');
    },

    async uom(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        ef_item_refresh_uom_factors_from_cached_item(row);
        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);
        frm.refresh_field('items');
    },

    async ef_uom(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        ef_item_refresh_uom_factors_from_cached_item(row);
        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);
        frm.refresh_field('items');
    },

    async qty(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);
        frm.refresh_field('items');
    },

    async rate(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);
        frm.refresh_field('items');
    },

    async item_tax_template(frm, cdt, cdn) {
        const row = locals[cdt][cdn];

        await ef_item_recalculate_row(frm, row);
        await ef_recalculate_totals(frm);

        frm.refresh_field('items');
    },
});

// -----------------------------
// Helpers
// -----------------------------

async function ef_item_apply_defaults_from_item(frm, row) {
    if (!row.item_code) return;

    // Fetch Item with UOM conversion table to compute factors reliably
    const r = await frappe.call({
        method: 'frappe.client.get',
        args: {
            doctype: 'Item',
            name: row.item_code
        }
    });

    const item = r && r.message ? r.message : null;
    if (!item) return;

    // Cache item on the row to avoid refetching on every qty/rate change
    // (Frappe keeps locals in memory for the current form)
    row.__ef_item_cache = item;

    if (!row.item_name) row.item_name = item.item_name || row.item_code;

    row.stock_uom = item.stock_uom || row.stock_uom;

    // Default UOM: if empty -> stock_uom
    if (!row.uom) row.uom = row.stock_uom;

    // Default eFactura UOM: if empty -> same as UOM
    if (!row.ef_uom) row.ef_uom = row.uom;

    ef_item_refresh_uom_factors_from_cached_item(row);

    // Optional: pull default Item Tax Template from Item
    if (!row.item_tax_template && item.taxes && item.taxes.length) {
        // item.taxes is a child table; in many setups it contains item_tax_template
        // Keep it defensive: only set if field exists
        const maybeTemplate = item.taxes[0].item_tax_template;
        if (maybeTemplate) row.item_tax_template = maybeTemplate;
    }

    // If we have tax template -> derive VAT rate
    if (row.item_tax_template && !row.ef_vat_rate) {
        await ef_item_apply_vat_rate_from_template(frm, row);
    }
}

function ef_item_refresh_uom_factors_from_cached_item(row) {
    const item = row.__ef_item_cache;
    if (!item) {
        // Fallback: assume 1 when no cache
        row.conversion_factor = row.conversion_factor || 1;
        row.ef_conversion_factor = row.ef_conversion_factor || 1;
        return;
    }

    // In ERPNext: conversion_factor is defined as "1 UOM = conversion_factor * stock_uom"
    row.conversion_factor = ef_item_get_conversion_factor(item, row.uom, item.stock_uom);
    row.ef_conversion_factor = ef_item_get_conversion_factor(item, row.ef_uom, item.stock_uom);

    if (!row.conversion_factor) row.conversion_factor = 1;
    if (!row.ef_conversion_factor) row.ef_conversion_factor = 1;
}

function ef_item_get_conversion_factor(item, uom, stock_uom) {
    if (!uom || !stock_uom) return 1;
    if (uom === stock_uom) return 1;

    const uoms = item.uoms || [];
    const found = uoms.find(d => d.uom === uom);
    return found && found.conversion_factor ? flt(found.conversion_factor) : 1;
}

async function ef_item_apply_vat_rate_from_template(frm, row) {
    if (!row.item_tax_template) {
        row.ef_vat_rate = 0;
        return;
    }

    if (frm.__item_tax_template !== undefined && frm.__item_tax_template[row.item_tax_template] !== undefined) {
        r = frm.__item_tax_template[row.item_tax_template];
    } else {
        try {            
            const r = await frappe.call({
                method: 'frappe.client.get',
                args: { doctype: 'Item Tax Template', name: row.item_tax_template }
            });

            if (frm.__item_tax_template == undefined) {
                frm.__item_tax_template = { };
            }
            frm.__item_tax_template[row.item_tax_template] = r;

            const tpl = r && r.message ? r.message : null;
            const taxes = tpl && tpl.taxes ? tpl.taxes : [];

            // Take first tax row as VAT rate (you can refine later if needed)
            if (taxes.length && taxes[0].tax_rate != null) {
                row.ef_vat_rate = cint(taxes[0].tax_rate);
            }
        } catch (e) {
            // fail silently
        }
    }

}

async function ef_item_recalculate_row(frm, row) {
    const qty = flt(row.qty || 0);
    const rate = flt(row.rate || 0);
    const efRateFactor = flt(frm.doc.ef_conversion_rate || 1);

    // Amounts in document currency
    row.amount = qty * rate;

    // UOM quantities
    row.stock_qty = qty * flt(row.conversion_factor || 1);

    const efConv = flt(row.ef_conversion_factor || 1);
    row.ef_qty = efConv ? (row.stock_qty / efConv) : qty;

    // Amounts in eFactura currency
    row.ef_rate = rate * efRateFactor;
    row.ef_amount = flt(row.amount || 0) * efRateFactor;

    // Rate per eFactura UOM
    const conv = flt(row.conversion_factor || 1);
    const stockRate = conv ? (rate / conv) : rate;
    row.ef_uom_rate = stockRate * efConv * efRateFactor;

    await ef_item_apply_net_vat_breakdown(frm, row);
}

async function ef_item_apply_net_vat_breakdown(frm, row) {
    await ef_item_apply_vat_rate_from_template(frm, row);
    
    const vatRate = flt(row.ef_vat_rate || 0);
    const qty = flt(row.qty || 0);

    const rate = flt(row.rate || 0);
    const amount = flt(row.amount || 0);

    let efRate = flt(row.ef_rate || 0);
    let efAmount = flt(row.ef_amount || 0);

    // No VAT at all
    if (!vatRate) {
        row.net_rate = rate;
        row.net_amount = amount;
        row.vat_amount = 0;

        row.ef_net_rate = efRate;
        row.ef_net_amount = efAmount;
        row.ef_vat_amount = 0;
        return;
    }

    const included = await ef_get_vat_included_in_rate(frm);

    if (included) {
        // -----------------------------
        // VAT is included in rate
        // ef_amount = GROSS
        // -----------------------------
        const divider = 1 + vatRate / 100;

        row.net_rate = divider ? (rate / divider) : rate;
        row.net_amount = divider ? (amount / divider) : amount;
        row.vat_amount = amount - row.net_amount;

        row.ef_net_rate = divider ? (efRate / divider) : efRate;
        row.ef_net_amount = divider ? (efAmount / divider) : efAmount;
        row.ef_vat_amount = efAmount - row.ef_net_amount;

    } else {
        // -----------------------------------
        // VAT is not included in rate
        // ef_rate / ef_amount = GROSS
        // -----------------------------------
        
        const vatAmount = amount * (vatRate / 100);
        const vatAmountEf = efAmount * (vatRate / 100);
        const vatRateEf = efRate * (vatRate / 100);

        row.net_rate = rate;
        row.net_amount = amount;
        row.vat_amount = vatAmount;

        row.ef_net_rate = efRate;
        row.ef_net_amount = efAmount;
        row.ef_vat_amount = vatAmountEf;

        row.ef_rate = efRate + vatRateEf;
        row.ef_amount = efAmount + vatAmountEf;
    }

    if (!qty) {
        row.net_rate = 0;
        row.ef_net_rate = 0;
    }
}



async function ef_recalculate_totals(frm) {
    const included = await ef_get_vat_included_in_rate(frm);

    let net_total = 0;
    let vat_total = 0;
    let total = 0;

    let ef_net_total = 0;
    let ef_vat_total = 0;
    let ef_total = 0;

    const rows = frm.doc.items || [];
    for (const row of rows) {
        const row_net = flt(row.net_amount || 0);

        // Document currency VAT/gross
        const row_amount = flt(row.amount || 0);
        const row_vat_rate = flt(row.ef_vat_rate || 0); // VAT rate is the same conceptually; stored as ef_vat_rate
        let row_vat = 0;
        let row_total = 0;

        if (row_vat_rate) {
            if (included) {
                // amount is gross
                row_total = row_amount;
                row_vat = row_total - row_net;
            } else {
                // amount is net, vat on top
                row_vat = row_amount * (row_vat_rate / 100);
                row_total = row_amount + row_vat;
            }
        } else {
            row_vat = 0;
            row_total = row_amount;
        }

        net_total += row_net;
        vat_total += row_vat;
        total += row_total;

        // eFactura currency totals (rows already kept as: ef_amount = gross, ef_net_amount = net, ef_vat_amount = vat)
        ef_net_total += flt(row.ef_net_amount || 0);
        ef_vat_total += flt(row.ef_vat_amount || 0);
        ef_total += flt(row.ef_amount || 0);
    }

    // Set parent totals
    frm.set_value('net_total', net_total);
    frm.set_value('vat_total', vat_total);
    frm.set_value('total', total);

    frm.set_value('ef_net_total', ef_net_total);
    frm.set_value('ef_vat_total', ef_vat_total);
    frm.set_value('ef_total', ef_total);
}

async function ef_recalculate_all_items_and_totals(frm) {
    const rows = frm.doc.items || [];
    for (const row of rows) {
        await ef_item_recalculate_row(frm, row);
    }
    await ef_recalculate_totals(frm);
    frm.refresh_field('items');
}

async function ef_get_vat_included_in_rate(frm) {
    // cache per form
    if (frm.__ef_vat_included_in_rate !== undefined) {
        return cint(frm.__ef_vat_included_in_rate);
    }

    try {
        // Singleton settings (лучше всего)
        const v = await frappe.db.get_single_value('eFactura Settings', 'vat_included_in_rate');
        frm.__ef_vat_included_in_rate = cint(v || 0);
        return frm.__ef_vat_included_in_rate;
    } catch (e) {
        frm.__ef_vat_included_in_rate = 0;
        return 0;
    }
}


// -----------------------------
// Grid currency labels (Items)
// -----------------------------
function ef_set_items_grid_currency_labels(frm) {
    // Document currency columns
    frm.set_currency_labels([
        'rate', 'amount', 'net_rate', 'net_amount'
    ], frm.doc.currency, 'items');

    // eFactura currency columns
    frm.set_currency_labels([
        'ef_rate', 'ef_amount', 'ef_uom_rate', 'ef_net_rate', 
        'ef_net_amount','ef_vat_amount'
    ], frm.doc.ef_currency, 'items');
}


// -----------------------------
// eFactura XML Signing (MoldSign)
// -----------------------------
const MOLDSIGN_BASE = "http://localhost:8999";

async function ms_fetch(urlOrPath, options = {}) {
  const url = urlOrPath.startsWith("http") ? urlOrPath : `${MOLDSIGN_BASE}${urlOrPath}`;

  const resp = await fetch(url, {
    method: options.method || "GET",
    headers: options.headers || {},
    body: options.body,
    mode: "cors"
  });

  const text = await resp.text();
  const contentType = resp.headers.get("content-type") || "";

  let data = null;
  if (text && contentType.includes("application/json")) {
    try { data = JSON.parse(text); } catch (e) {}
  }

  return { resp, text, data };
}

async function ms_ping() {
  // Minimal check: certificates endpoint.
  const { resp, text } = await ms_fetch("/certificates?private_only=true", {
    headers: { "Accept": "application/json" }
  });

  if (!resp.ok) {
    throw new Error(`MoldSign not available: HTTP ${resp.status} ${text || ""}`.trim());
  }
}

async function ms_get_private_certs() {
  const { resp, data, text } = await ms_fetch("/certificates?private_only=true", {
    headers: { "Accept": "application/json" }
  });

  if (!resp.ok) {
    throw new Error(`MoldSign certificates error: HTTP ${resp.status} ${text || ""}`.trim());
  }

  const list = data?.certificateModel || [];
  return list.filter(c => c.privateKeyPresent);
}

async function ms_start_sign_session({ hash_base64, certificate }) {
  const payload = {
    algorithm: "SHA-1",
    signatureType: "Embedded", //"Detached",
    signFormat: "XAdES-T", //"XAdES-BES",
    contentType: "Text",
    data: hash_base64,
    certificate: certificate
  };

  const { resp, text } = await ms_fetch("/sign/data", {
    method: "POST",
    headers: {
      "Accept": "application/json",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
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
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function ms_poll_result(location, { timeout_ms = 120000, interval_ms = 800 } = {}) {
  const started = Date.now();

  while (true) {
    if (Date.now() - started > timeout_ms) {
      throw new Error("MoldSign signing timeout.");
    }

    const { resp, data, text } = await ms_fetch(location, {
      headers: { "Accept": "application/json" }
    });

    if (resp.ok) {
      // Success - return raw.
      return {
        status: resp.status,
        headers: {
          error: resp.headers.get("error"),
          sessionId: resp.headers.get("sessionId"),
          location: resp.headers.get("Location")
        },
        data: data,
        text: text
      };
    }

    // Cancel / wrong PIN / user closed dialog usually returns 4xx.
    if (resp.status >= 400 && resp.status < 500) {
      const errHeader = resp.headers.get("error");
      const msg = errHeader || text || `HTTP ${resp.status}`;
      throw new Error(`MoldSign signing failed: ${msg}`.trim());
    }

    // For 5xx or transient - keep trying.
    await sleep(interval_ms);
  }
}

async function choose_certificate_dialog(certs) {
  const options = certs.map(c => ({
    label: c.certificateName,
    value: c.certificateId
  }));

  return new Promise((resolve, reject) => {
    const d = new frappe.ui.Dialog({
      title: __("Select certificate"),
      fields: [{
        fieldname: "cert",
        fieldtype: "Select",
        label: __("Certificate"),
        options: options,
        default: options[0]?.value || null,
        reqd: 1
      }],
      primary_action_label: __("Sign"),
      primary_action: () => {
        const certId = d.get_value("cert");
        d.hide();
        const selected = certs.find(c => c.certificateId === certId) || certs[0];
        resolve(selected);
      }
    });

    d.set_secondary_action(() => {
      d.hide();
      reject(new Error("Signing cancelled."));
    });

    d.show();
  });
}

async function sign_xml_moldsign(frm) {
  try {
    frappe.dom.freeze(__("Signing via MoldSign..."));

    // 0) Check MoldSign is reachable
    await ms_ping();

    // 1) Fetch XML from backend
    const r1 = await frappe.call({
      method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.get_for_sign",
      args: { efactura_name: frm.doc.name }
    });

    const xml_base64 = r1.message?.xml_base64;
    const hash_base64 = r1.message?.hash_base64;
    if (!xml_base64 || !hash_base64) {
      throw new Error("Backend did not return XML properties.");
    }

    // 2) Select certificate
    const certs = await ms_get_private_certs();
    if (!certs.length) {
      throw new Error("No private certificates found in MoldSign.");
    }

    frappe.dom.unfreeze();
    const selected_cert = await choose_certificate_dialog(certs);
    frappe.dom.freeze(__("Signing via MoldSign..."));

    // 3) Start session
    const location = await ms_start_sign_session({
      hash_base64: hash_base64,
      certificate: selected_cert,
    });

    // 4) Poll for result (user will be prompted by MoldSign)
    const result = await ms_poll_result(location);
    if (result && result.data && result.data.base64File) {
        frappe.show_alert({ message: __("Signed successfully"), indicator: "green" });
    }

    // 5) Save result on backend (new method you add below)
    const result2 = await frappe.call({
      method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.process_signed_xml",
      args: {
        name: frm.doc.name,
        signature: result.data.base64File,
        content: xml_base64,
      }
    });

    frappe.show_alert({ message: result2.message.message, indicator: "green" });
    frm.reload_doc();

  } catch (e) {
    frappe.msgprint({
      title: __("Signing error"),
      indicator: "red",
      message: e.message || String(e)
    });
  } finally {
    frappe.dom.unfreeze();
  }
}