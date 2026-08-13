// Copyright (c) 2025, Evgheni Nemerenco and contributors
// For license information, please see license.txt

frappe.ui.form.on('eFactura Settings', {
    refresh(frm) {
        set_options_for_idno_selects(frm);
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
                                    __('Found {0}, created {1}, updated {2}, details {3}, errors {4}', [
                                        r.message.found,
                                        r.message.created,
                                        r.message.updated,
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
