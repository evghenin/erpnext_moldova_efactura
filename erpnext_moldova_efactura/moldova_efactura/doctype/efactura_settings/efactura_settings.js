// Copyright (c) 2025, Evgheni Nemerenco and contributors
// For license information, please see license.txt

frappe.ui.form.on('eFactura Settings', {
    refresh(frm) {
        set_options_for_idno_selects(frm);
        const company_account_query = (doc, cdt, cdn) => {
            const row = locals[cdt][cdn];
            const filters = { is_group: 0 };
            if (row.company) {
                filters.company = row.company;
            }
            return { filters };
        };
        frm.set_query("buying_vat_account", "company_settings", company_account_query);
        frm.set_query("selling_vat_account", "sales_tax_settings", company_account_query);
        const company_template_query = (doc, cdt, cdn) => {
            const row = locals[cdt][cdn];
            const filters = {};
            if (row.company) {
                filters.company = row.company;
            }
            return { filters };
        };
        frm.set_query("taxes_and_charges", "company_settings", company_template_query);
        frm.set_query("sales_taxes_and_charges", "sales_tax_settings", company_template_query);
        frm.set_query("item_tax_template", "outgoing_item_tax_templates", () => ({
            filters: { disabled: 0 },
        }));
        frm.add_custom_button(__('Fetch Buyer Invoices'), () => {
            frappe.prompt(
                [
                    {
                        fieldname: 'lookback_days',
                        fieldtype: 'Int',
                        label: __('Lookback days'),
                        default: 180,
                        reqd: 1,
                    },
                ],
                (values) => {
                    frappe.call({
                        method: 'erpnext_moldova_efactura.tasks.buyer_sync.fetch_buyer_invoices',
                        args: { lookback_days: values.lookback_days },
                        freeze: true,
                        callback(r) {
                            if (r.message) {
                                frappe.msgprint(
                                    __('Found {0}, created {1}, updated {2}, skipped {3}, details {4}, errors {5}', [
                                        r.message.found,
                                        r.message.created,
                                        r.message.updated,
                                        r.message.skipped || 0,
                                        r.message.details_loaded,
                                        r.message.errors,
                                    ])
                                );
                            }
                        },
                    });
                },
                __('Fetch Buyer Invoices'),
                __('Fetch')
            );
        });
        frm.add_custom_button(__('Fetch Supplier Invoices'), () => {
            frappe.prompt(
                [
                    {
                        fieldname: 'lookback_days',
                        fieldtype: 'Int',
                        label: __('Lookback days'),
                        default: 180,
                        reqd: 1,
                    },
                ],
                (values) => {
                    frappe.call({
                        method: 'erpnext_moldova_efactura.tasks.supplier_sync.fetch_supplier_invoices',
                        args: { lookback_days: values.lookback_days },
                        freeze: true,
                        callback(r) {
                            if (r.message) {
                                frappe.msgprint(
                                    __('Found {0}, created {1}, updated {2}, skipped {3}, details {4}, errors {5}', [
                                        r.message.found,
                                        r.message.created,
                                        r.message.updated,
                                        r.message.skipped || 0,
                                        r.message.details_loaded,
                                        r.message.errors,
                                    ])
                                );
                            }
                        },
                    });
                },
                __('Fetch Supplier Invoices'),
                __('Fetch')
            );
        });
    }
});

function set_options_for_idno_selects(frm) {
    // Company
    frappe.model.with_doctype('Company', () => {
        const fields = frappe.meta.get_docfields('Company');
        const data_fields = fields
            .filter(df => df.fieldtype === 'Data')
            .map(df => df.fieldname);

        frm.set_df_property('company_idno_field', 'options', [''].concat(data_fields));
    });

    // Customer
    frappe.model.with_doctype('Customer', () => {
        const fields = frappe.meta.get_docfields('Customer');
        const data_fields = fields
            .filter(df => df.fieldtype === 'Data')
            .map(df => df.fieldname);

        frm.set_df_property('customer_idno_field', 'options', [''].concat(data_fields));
    });

    // Supplier
    frappe.model.with_doctype('Supplier', () => {
        const fields = frappe.meta.get_docfields('Supplier');
        const data_fields = fields
            .filter(df => df.fieldtype === 'Data')
            .map(df => df.fieldname);

        frm.set_df_property('supplier_idno_field', 'options', [''].concat(data_fields));
    });
}

frappe.ui.form.on("eFactura Company Setting", {
    company(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.buying_vat_account) {
            frappe.db.get_value("Account", row.buying_vat_account, "company", (r) => {
                if (r && r.company && r.company !== row.company) {
                    frappe.model.set_value(cdt, cdn, "buying_vat_account", "");
                }
            });
        }
        if (row.taxes_and_charges) {
            frappe.db.get_value(
                "Purchase Taxes and Charges Template",
                row.taxes_and_charges,
                "company",
                (r) => {
                    if (r && r.company && r.company !== row.company) {
                        frappe.model.set_value(cdt, cdn, "taxes_and_charges", "");
                    }
                }
            );
        }
    },
});

frappe.ui.form.on("eFactura Sales Tax Setting", {
    company(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.selling_vat_account) {
            frappe.db.get_value("Account", row.selling_vat_account, "company", (r) => {
                if (r && r.company && r.company !== row.company) {
                    frappe.model.set_value(cdt, cdn, "selling_vat_account", "");
                }
            });
        }
        if (row.sales_taxes_and_charges) {
            frappe.db.get_value(
                "Sales Taxes and Charges Template",
                row.sales_taxes_and_charges,
                "company",
                (r) => {
                    if (r && r.company && r.company !== row.company) {
                        frappe.model.set_value(cdt, cdn, "sales_taxes_and_charges", "");
                    }
                }
            );
        }
    },
});
