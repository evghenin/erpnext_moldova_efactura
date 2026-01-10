frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.docstatus !== 1) return;

        const status = frm.doc.fiscal_status;
        if (status) {
            const color_map = {
                "Pending": "red",
                "In Progress": "orange",
                "Partial": "orange",
                "Completed": "green",
                "Failed": "red",
                "Not Required": "gray",
                "Not Applicable": "gray",
            };

            const color = color_map[status] || "gray";

            frm.page.set_indicator(
                __('Fiscalization: {0}', [__(status)]),
                color
            );
        }


        frm.add_custom_button(__("eFactura"), () => {
            frappe.model.open_mapped_doc({
                method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.make_efactura_from_sales_invoice",
                frm: frm
            });
        }, __("Create"));

        frm.add_custom_button(__('Actualize Fiscal Status'), () => {
            frappe.call({
                method: 'erpnext_moldova_efactura.api.fiscal_status.actualize_sales_invoice_fiscal_status',
                args: { sales_invoice: frm.doc.name },
                freeze: true,
                callback(r) {
                    if (r.message) {
                    frappe.show_alert({
                        message: __('Fiscal status updated.'),
                        indicator: 'green'
                    });
                    frm.reload_doc();
                    }
                }
            });
        }, __('Actions'));

        
    }
});
