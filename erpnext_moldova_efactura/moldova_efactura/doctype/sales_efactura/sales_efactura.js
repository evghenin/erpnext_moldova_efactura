// Copyright (c) 2025, Evgheni Nemerenco and contributors
// For license information, please see license.txt

frappe.ui.form.on('Sales eFactura', {
    setup: function (frm) {
        frm.set_indicator_formatter("item_code", function (doc) {
            return doc.docstatus == 1 || doc.stock_qty <= doc.available_stock_qty ? "green" : "red";
        });
        ensure_customer_idno_field(frm);
        ensure_supplier_idno_field(frm);
        ensure_fiscal_territory(frm);
    },

    before_submit(frm) {
        if (cint(frm.doc.si_qty_overage_confirmed)) {
            frm._ef_in_submit = true;
            return;
        }

        return frappe
            .call({
                method: "erpnext_moldova_efactura.utils.qty_guard.check_si_qty_overage",
                args: { doc: frm.doc },
            })
            .then((r) => {
                const data = r.message || {};
                if (!data.overages || !data.overages.length) {
                    frm._ef_in_submit = true;
                    return;
                }
                return show_si_qty_overage_dialog(frm, data).then(() => {
                    frm._ef_in_submit = !!frappe.validated;
                });
            })
            .catch(() => {
                frappe.validated = false;
                frm._ef_in_submit = false;
            });
    },

    before_save(frm) {
        if (cint(frm.doc.docstatus) !== 0 || frm._ef_in_submit) {
            return;
        }
        if (cint(frm.doc.si_qty_overage_confirmed)) {
            return;
        }

        return frappe
            .call({
                method: "erpnext_moldova_efactura.utils.qty_guard.check_si_qty_overage",
                args: { doc: frm.doc, include_drafts: 1 },
            })
            .then((r) => {
                const data = r.message || {};
                if (!data.overages || !data.overages.length) {
                    return;
                }
                return show_si_qty_overage_dialog(frm, data);
            })
            .catch(() => {
                frappe.validated = false;
            });
    },

    refresh(frm) {
        frm._ef_in_submit = false;
        setup_sales_invoice_query(frm);
        update_company_bank_account(frm);
        update_transporter_party(frm);
        apply_currency_rules(frm, { fetch_rate: false });
        ef_set_items_grid_currency_labels(frm);
        sync_sef_party_type(frm);
        autofillEfDetails(frm, "supplier");
        autofillEfDetails(frm, "customer");
        autofillEfDetails(frm, "transporter");
        ensure_customer_idno_field(frm);
        ensure_supplier_idno_field(frm);
        ensure_fiscal_territory(frm);
        setup_new_party_from_factura(frm);
        setup_new_item_from_factura(frm);
        update_customer(frm);

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
					if (!frm.doc.customer_party) {
						frappe.throw({
							title: __("Mandatory"),
							message: __("Please select a Customer first."),
						});
					}
					erpnext.utils.map_current_doc({
						method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.make_efactura_from_delivery_note",
						source_doctype: "Delivery Note",
						target: frm,
						setters: {
							customer: frm.doc.customer_party,
						},
						get_query_filters: {
							docstatus: 1,
							status: ["not in", ["Cancelled"]],
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
                    const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.download_xml?efactura_name=${encodeURIComponent(frm.doc.name)}`;
                    const url = frappe.urllib.get_full_url(endpoint);
                    window.open(url, "_blank");
                },
                __("eFactura Actions")
            );
        }


        if (
            !frm.is_new() && 
            frm.doc.docstatus === 1 &&
            frm.doc.ef_status !== "Pending Registration"
        ) {
            frm.add_custom_button(
                __("Download PDF"), 
                function () {
                    const endpoint = `/api/method/erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.download_pdf?efactura_name=${encodeURIComponent(frm.doc.name)}`;
                    const url = frappe.urllib.get_full_url(endpoint);
                    window.open(url, "_blank");
                },
                __("eFactura Actions")
            );

            if (frm.has_perm("write")) {
            frm.add_custom_button(
                __("Update Status"),
                function () {
                    frappe.call({
                        method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.update_ef_status",
                        args: { efactura_name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Updating e-Factura status..."),
                        callback: (r) => {
                            frappe.show_alert({
                                message: __("e-Factura status updated successfully."),
                                indicator: "green",
                            }, 5);
                            frm.reload_doc();
                        },
                    });
                },
                __("eFactura Actions")
            );
            }
        }

        if (
            !frm.is_new() &&
            frm.doc.docstatus === 1 &&
            frm.has_perm("write") &&
            frm.doc.ef_series &&
            frm.doc.ef_number &&
            [
                "Signed by Supplier",
                "Sent to Customer",
                "Accepted by Customer",
                "Signed by Customer",
                "Rejected by Customer",
                "Transportation",
            ].includes(frm.doc.ef_status)
        ) {
            frm.add_custom_button(
                __("Cancel"),
                () => {
                    frappe.prompt(
                        [
                            {
                                fieldname: "reason",
                                fieldtype: "Small Text",
                                label: __("Cancellation Reason"),
                                reqd: 1,
                                default: frm.doc.cancellation_reason || "",
                            },
                        ],
                        (values) => {
                            frappe.call({
                                method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.cancel_invoice",
                                args: {
                                    name: frm.doc.name,
                                    reason: values.reason,
                                },
                                freeze: true,
                                freeze_message: __("Canceling e-Factura..."),
                                callback() {
                                    frappe.show_alert({
                                        message: __("e-Factura canceled."),
                                        indicator: "orange",
                                    });
                                    frm.reload_doc();
                                },
                            });
                        },
                        __("Cancel e-Factura"),
                        __("Cancel")
                    );
                },
                __("eFactura Actions")
            );
        }

        if (
            !frm.is_new() && 
            frm.doc.docstatus === 1 && 
            frm.doc.ef_status === "Pending Registration" &&
            frm.has_perm("write")
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
                                method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.update_dates",
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
                        method: "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.send_unsigned",
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

        const invoiceLinked = !!frm.doc.sales_invoice;
        if (
            !frm.is_new() &&
            frm.doc.docstatus !== 2 &&
            !(frm.doc.docstatus === 1 && invoiceLinked) &&
            (frm.doc.items || []).length &&
            frm.has_perm("write")
        ) {
            const createMenu = __("Create");
            if (frappe.model.can_create("Sales Order")) {
                frm.add_custom_button(
                    __("Sales Order"),
                    () => create_selling_doc_from_sef(
                        frm,
                        __("Sales Order"),
                        "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.make_sales_order"
                    ),
                    createMenu
                );
            }
            if (frappe.model.can_create("Sales Invoice") && !invoiceLinked) {
                frm.add_custom_button(
                    __("Sales Invoice"),
                    () => create_selling_doc_from_sef(
                        frm,
                        __("Sales Invoice"),
                        "erpnext_moldova_efactura.moldova_efactura.doctype.sales_efactura.sales_efactura.make_sales_invoice"
                    ),
                    createMenu
                );
            }
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

                const taxpayer_type = frm.doc[`ef_${party_type}_taxpayer_type`];
                if (taxpayer_type) {
                    html_content += `<tr>
                        <td><b>${__("Taxpayer Type")}:</b></td>
                        <td>${__(taxpayer_type, null, "Sales eFactura")}</td>
                    </tr>`;
                }

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
        if (frm.doc.type == "Transfer") {
            frm.set_value("naming_series", "ACC-SEF-.YYYY.-");
        } else if (frm.doc.type == "Non-Transfer") {
            frm.set_value("naming_series", "ACC-SEF-NT-.YYYY.-");
        }
        sync_sef_party_type(frm);
    },

    async sales_invoice(frm) {
        await validate_sales_invoice_customer(frm);
        update_customer(frm, { sync_from_si: true });
    },

    company(frm) {
        frm.set_value('sales_invoice', null);
        setup_sales_invoice_query(frm);

        if (!frm.doc.company) {
            frm.set_value('company_bank_account', null);
        } else {
            set_default_company_bank_account(frm);
        }
        update_company_bank_account(frm);
        update_transporter_party(frm, { apply_defaults: true });
    },

    customer_party(frm) {
        setup_sales_invoice_query(frm);
    },

    currency(frm) {
        apply_currency_rules(frm, { fetch_rate: true });
        ef_set_items_grid_currency_labels(frm);
    },

    ef_currency(frm) {
        apply_currency_rules(frm, { fetch_rate: true });
        ef_set_items_grid_currency_labels(frm);
    },

    issue_date(frm) {
        apply_currency_rules(frm, { fetch_rate: true });
    },

    transporter_party_type(frm) {
        // If type is unset: clear dependent fields
        if (!frm.doc.transporter_party_type) {
            frm.set_value('transporter_party', null);
        } else {
            // If switched to Company: set transporter_party from company (if available)
            update_transporter_party(frm, { apply_defaults: true });
        }
    },

    transporter_party(frm) {
        update_transporter_party(frm);
    },

    ef_conversion_rate: async function(frm) {
        await ef_recalculate_all_items_and_totals(frm);
    },
});

async function validate_sales_invoice_customer(frm) {
    const si = frm.doc.sales_invoice;
    if (!si) {
        return;
    }
    if ((frm.doc.customer_party_type || "Customer") !== "Customer" || !frm.doc.customer_party) {
        await frm.set_value('sales_invoice', null);
        frappe.throw(__('Select Customer first'));
    }

    let si_customer = null;
    try {
        const r = await frappe.db.get_value('Sales Invoice', si, 'customer');
        si_customer = r && r.customer;
    } catch (e) {
        return;
    }
    if (si_customer && si_customer !== frm.doc.customer_party) {
        await frm.set_value('sales_invoice', null);
        frappe.throw(
            __('Sales Invoice {0} does not belong to Customer {1}', [si, frm.doc.customer_party])
        );
    }
}

function setup_sales_invoice_query(frm) {
    frm.toggle_enable('sales_invoice', 1);
    frm.set_df_property('sales_invoice', 'description', '');

    frm.set_query('sales_invoice', function () {
        const filters = { docstatus: 1 };
        if (frm.doc.company) {
            filters.company = frm.doc.company;
        }
        if (frm.doc.customer_party_type === "Customer" && frm.doc.customer_party) {
            filters.customer = frm.doc.customer_party;
        }
        return { filters };
    });
}

async function apply_currency_rules(frm, opts) {
    const fetch_rate = !!(opts && opts.fetch_rate) || frm.is_new();
    const cur = frm.doc.currency;
    const efCur = frm.doc.ef_currency;

    // If currencies are missing, do not proceed
    if (!cur || !efCur) {
        frm.set_df_property('ef_conversion_rate', 'read_only', 0);
        return;
    }

    // Same currency: rate = 1 and read-only
    if (cur === efCur) {
        if (fetch_rate && flt(frm.doc.ef_conversion_rate) !== 1) {
            await frm.set_value('ef_conversion_rate', 1);
        }
        frm.set_df_property('ef_conversion_rate', 'read_only', 1);
        return;
    }

    // Different currencies: editable
    frm.set_df_property('ef_conversion_rate', 'read_only', 0);
    if (!fetch_rate) {
        return;
    }

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
        if (rate && rate > 0 && flt(frm.doc.ef_conversion_rate) !== rate) {
            await frm.set_value('ef_conversion_rate', rate);
        }
    } catch (e) {
        // Leave editable for manual input
    }
}

function update_company_bank_account(frm) {
    const company = frm.doc.company;
    frm.toggle_enable('company_bank_account', !!company);

    if (company && frm.is_new()) {
        set_default_company_bank_account(frm);
    }

    frm.set_query('company_bank_account', function() {
        const company = frm.doc.company;
        if (!company) {
            return { filters: { name: ['=', ''] } };
        }
        return {
            filters: {
                is_company_account: 1,
                company: company,
            },
        };
    });
}

async function set_default_company_bank_account(frm) {
    const company = frm.doc.company;
    if (!company) return;

    if (frm.doc.company_bank_account) {
        try {
            const r = await frappe.call({
                method: 'frappe.client.get_value',
                args: {
                    doctype: 'Bank Account',
                    filters: { name: frm.doc.company_bank_account },
                    fieldname: ['company', 'is_company_account']
                }
            });

            const ba = r && r.message ? r.message : null;
            if (ba && cint(ba.is_company_account) === 1 && ba.company === company) {
                return;
            }
        } catch (e) {
            // Ignore and continue with default selection
        }
    }

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
            await frm.set_value('company_bank_account', rowsDefault[0].name);
            return;
        }
    } catch (e) {
        // Ignore and try fallback
    }
}

function expected_sef_party_type(frm) {
    return frm.doc.type === "Non-Transfer" ? "Supplier" : "Customer";
}

function sync_sef_party_type(frm) {
    const expected = expected_sef_party_type(frm);
    if ((frm.doc.customer_party_type || "") !== expected) {
        frm.doc.customer_party_type = expected;
        const field = frm.fields_dict.customer_party_type;
        if (field) {
            field.last_value = expected;
            if (typeof field.set_input === "function") {
                field.set_input(expected);
            }
        }
    }
    if (frm.fields_dict.customer_party) {
        frm.set_df_property("customer_party", "hidden", 0);
        frm.refresh_field("customer_party");
    }
}

function update_customer(frm, opts) {
    const hasRefName = !!frm.doc.sales_invoice;

    if (hasRefName && (frm.doc.customer_party_type || "Customer") === "Customer") {
        frm.set_df_property('customer_party', 'read_only', 1);
        if (opts && opts.sync_from_si) {
            update_customer_from_reference(frm);
        }
    } else {
        frm.set_df_property('customer_party', 'read_only', 0);
    }
}

async function update_customer_from_reference(frm) {
    if (!frm.doc.sales_invoice) {
        return;
    }

    try {
        const r = await frappe.call({
            method: 'frappe.client.get_value',
            args: {
                doctype: 'Sales Invoice',
                filters: { name: frm.doc.sales_invoice },
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
function update_transporter_party(frm, opts) {
    const apply_defaults = !!(opts && opts.apply_defaults);
    const isCompanyTransporter = frm.doc.transporter_party_type === 'Company';
    const company = frm.doc.company;
    const hasType = !!frm.doc.transporter_party_type;

    // Disable transporter_party if transporter_party_type is not selected
    if (!hasType) {
        frm.set_df_property('transporter_party', 'read_only', 1);
    } else if (isCompanyTransporter) {
        if (company) {
            if (apply_defaults && frm.doc.transporter_party !== company) {
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
        } else if (frm.doc.transporter_party_type === 'Customer' && frm.doc.customer_party) {
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

function normalize_party_title(name) {
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

function ensure_customer_idno_field(frm) {
    if (frm._customer_idno_field !== undefined) {
        return;
    }
    frappe.db.get_single_value("eFactura Settings", "customer_idno_field").then((field) => {
        frm._customer_idno_field = field;
    });
}

function ensure_supplier_idno_field(frm) {
    if (frm._supplier_idno_field !== undefined) {
        return;
    }
    frappe.db.get_single_value("eFactura Settings", "supplier_idno_field").then((field) => {
        frm._supplier_idno_field = field;
    });
}

function ensure_fiscal_territory(frm) {
    if (frm._fiscal_territory !== undefined) {
        return;
    }
    frappe.db.get_single_value("eFactura Settings", "fiscal_territory").then((value) => {
        frm._fiscal_territory = value || "";
    });
}

function customer_route_options_from_factura(frm) {
    const opts = {};
    const partyName = normalize_party_title(frm.doc.ef_customer_name);
    const ptype = frm.doc.customer_party_type || "Customer";
    if (partyName) {
        opts.name_field = partyName;
        if (ptype === "Supplier") {
            opts.supplier_name = partyName;
        } else {
            opts.customer_name = partyName;
        }
    }
    const idnoField = ptype === "Supplier" ? frm._supplier_idno_field : frm._customer_idno_field;
    const idno = (frm.doc.ef_customer_idno || "").trim();
    if (idnoField && idno) {
        opts[idnoField] = idno;
    }
    const taxpayerType = frm.doc.ef_customer_taxpayer_type;
    if (ptype !== "Supplier") {
        if (taxpayerType === "Individual") {
            opts.customer_type = "Individual";
        } else if (taxpayerType === "Company" || taxpayerType === "Non-Resident") {
            opts.customer_type = "Company";
        }
    }
    if (frm._fiscal_territory) {
        opts.territory = frm._fiscal_territory;
    }
    return opts;
}

function setup_new_party_from_factura(frm) {
    const field = frm.fields_dict.customer_party;
    const df = frm.get_docfield("customer_party");
    if (!field || !df || frm.doc.docstatus !== 0) {
        return;
    }

    df.get_route_options_for_new_doc = () => customer_route_options_from_factura(frm);

    if (field._ef_new_doc_wrapped) {
        return;
    }
    field._ef_new_doc_wrapped = true;
    const original_new_doc = field.new_doc.bind(field);
    field.new_doc = function () {
        const result = original_new_doc();
        const customerName = normalize_party_title(frm.doc.ef_customer_name);
        if (customerName && frappe.route_options) {
            Object.assign(frappe.route_options, customer_route_options_from_factura(frm));
            frappe.route_options.name_field = customerName;
        }
        return result;
    };
}

function item_title_from_row(row) {
    return ((row && (row.item_name || row.ef_item_code)) || "").trim();
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
    hook_items_grid_refresh(frm);
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

function hook_items_grid_refresh(frm) {
    const grid = frm.fields_dict.items && frm.fields_dict.items.grid;
    if (!grid || grid._ef_item_new_doc_hooked) {
        return;
    }
    grid._ef_item_new_doc_hooked = true;
    const original_refresh = grid.refresh.bind(grid);
    grid.refresh = function (...args) {
        const result = original_refresh(...args);
        wrap_item_code_grid_controls(frm);
        return result;
    };
}

frappe.ui.form.on('Sales eFactura Item', {
    async item_code(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        await ef_item_apply_defaults_from_item(frm, row);
        if (!ef_is_sfs_sourced(frm)) {
            await ef_item_recalculate_row(frm, row);
            await ef_recalculate_totals(frm);
        }
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

    items_add: async function(frm) {
        await ef_recalculate_totals(frm);
    },

    items_remove: async function(frm) {
        await ef_recalculate_totals(frm);
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

function ef_is_sfs_sourced(frm) {
    return frm.doc.ef_status && frm.doc.ef_status !== "Pending Registration" && frm.doc.ef_series && frm.doc.ef_number;
}

async function ef_item_apply_vat_rate_from_template(frm, row) {
    if (!row.item_tax_template) {
        // Keep the XML VAT rate on invoices loaded from SFS.
        return;
    }
    if (ef_is_sfs_sourced(frm) && flt(row.ef_vat_rate)) {
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


function describe_unmapped_sef_row(row, currency) {
    const name = row.item_name || row.ef_item_code || row.item_code || __("row {0}", [row.idx]);
    const qty = flt(row.qty || row.ef_qty || 0);
    const rate = flt(row.rate || 0);
    return __("Row {0}: {1} — qty {2}, rate {3} {4}", [
        row.idx,
        name,
        qty,
        rate,
        currency || "",
    ]);
}

function assert_sef_ready_to_create(frm, actionLabel) {
    if (!frm.doc.customer_party) {
        frappe.throw({
            title: __("Mandatory"),
            message: __("Customer is required to create {0}", [actionLabel]),
        });
    }
    if (!(frm.doc.items || []).length) {
        frappe.throw({
            title: __("Mandatory"),
            message: __("No items on Sales eFactura — fetch details first"),
        });
    }
    const unmapped = (frm.doc.items || []).filter((row) => !row.item_code);
    if (unmapped.length) {
        const items_html = unmapped
            .map((row) => `<li>${frappe.utils.escape_html(describe_unmapped_sef_row(row, frm.doc.currency))}</li>`)
            .join("");
        frappe.throw({
            title: __("Map all items"),
            message: __("Map all items before creating {0}", [actionLabel]) + `<ul>${items_html}</ul>`,
        });
    }
    for (const row of frm.doc.items || []) {
        if (!row.uom) {
            frappe.throw({
                title: __("Mandatory"),
                message: __("UOM is required for row {0}", [row.idx]),
            });
        }
        if (!flt(row.qty)) {
            frappe.throw({
                title: __("Mandatory"),
                message: __("Quantity is required for row {0}", [row.idx]),
            });
        }
    }
}

function create_selling_doc_from_sef(frm, actionLabel, method) {
    assert_sef_ready_to_create(frm, actionLabel);
    const open = () => {
        frappe.model.open_mapped_doc({
            method: method,
            frm: frm,
        });
    };
    if (frm.is_dirty()) {
        return frm.save().then(open);
    }
    return open();
}

async function sign_xml_moldsign(frm) {
    try {
        await erpnext_moldova_efactura.moldsign.sign_sales_efactura(frm.doc.name);
        frm.reload_doc();
    } catch (e) {
        frappe.msgprint({
            title: __("Signing error"),
            indicator: "red",
            message: e.message || String(e),
        });
    }
}

function show_si_qty_overage_dialog(frm, data) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (ok) => {
            if (settled) {
                return;
            }
            settled = true;
            if (!ok) {
                frappe.validated = false;
            }
            resolve();
        };

        const saveMode = data.mode === "save";
        const dialog = new frappe.ui.Dialog({
            title: data.block
                ? __("Quantity exceeds Sales Invoice")
                : __("Quantity exceeds Sales Invoice — confirmation required"),
            indicator: data.block ? "red" : "orange",
            fields: [
                {
                    fieldtype: "HTML",
                    fieldname: "overage_html",
                    options: data.message || "",
                },
            ],
            onhide() {
                finish(false);
            },
        });

        if (data.block) {
            dialog.set_primary_action(__("Close"), () => {
                finish(false);
                dialog.hide();
            });
        } else {
            dialog.set_primary_action(saveMode ? __("Save Anyway") : __("Submit Anyway"), () => {
                frm.doc.si_qty_overage_confirmed = 1;
                finish(true);
                dialog.hide();
            });
            dialog.set_secondary_action_label(__("Cancel"));
            dialog.set_secondary_action(() => {
                finish(false);
                dialog.hide();
            });
        }

        dialog.show();
    });
}