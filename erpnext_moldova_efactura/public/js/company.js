frappe.ui.form.on('Company', {
    refresh(frm) {
        // Limit VAT Account to tax accounts only (non-group) of currenct company
        frm.set_query('vat_account', function() {
            return {
                filters: {
                    account_type: 'Tax',
                    is_group: 0,
                    company: frm.doc.name
                }
            };
        });
    }
});