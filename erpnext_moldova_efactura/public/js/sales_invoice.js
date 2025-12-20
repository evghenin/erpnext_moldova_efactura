frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.is_new()) return;
        if (frm.doc.docstatus !== 1) return;


        frm.add_custom_button(__("eFactura"), () => {
            frappe.model.open_mapped_doc({
                method: "erpnext_moldova_efactura.moldova_efactura.doctype.efactura.efactura.make_efactura_from_sales_invoice",
                frm: frm
            });
        }, __("Create"));
    }
});