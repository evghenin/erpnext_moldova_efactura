frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        const sefName = frm.doc.sales_efactura;
        if (sefName && frappe.model.can_read("Sales eFactura")) {
            frm.add_custom_button(
                sefName,
                () => {
                    frappe.model.clear_doc("Sales eFactura", sefName);
                    frappe.set_route("Form", "Sales eFactura", sefName);
                },
                __("Sales eFactura")
            );
        }

        if (frm.doc.docstatus !== 1) return;

        erpnext_moldova_efactura.fiscal_status.set_form_indicator(frm, frm.doc.fiscal_status);


        if (frappe.model.can_create("Sales eFactura")) {
            frm.add_custom_button(__("Sales eFactura"), () => {
                frappe.model.open_mapped_doc({
                    method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.make_efactura_from_sales_invoice",
                    frm: frm
                });
            }, __("Create"));
        }

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

        
    },

    on_submit(frm) {
        offer_return_to_sales_efactura(frm);
    },
});

function offer_return_to_sales_efactura(frm) {
    if (!frm.doc || cint(frm.doc.docstatus) !== 1) {
        return;
    }
    if (!frappe.model.can_read("Sales eFactura")) {
        return;
    }
    const name = frm.doc.sales_efactura;
    if (!name) {
        return;
    }
    frappe.db.get_value("Sales eFactura", name, "docstatus").then((r) => {
        if (cint(r && r.message && r.message.docstatus) !== 0) {
            return;
        }
        const dialog = new frappe.ui.Dialog({
            title: __("Return to Sales eFactura"),
            primary_action_label: __("Open e-Factura"),
            primary_action() {
                dialog.hide();
                frappe.model.clear_doc("Sales eFactura", name);
                frappe.set_route("Form", "Sales eFactura", name);
            },
            secondary_action_label: __("Stay here"),
            secondary_action() {
                dialog.hide();
            },
        });
        dialog.$body.append(
            `<p class="frappe-confirm-message">${__(
                "Sales Invoice {0} is submitted. Open Sales eFactura {1} to submit it?",
                [frappe.utils.escape_html(frm.doc.name), frappe.utils.escape_html(name)]
            )}</p>`
        );
        dialog.show();
    });
}
