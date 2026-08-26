# Copyright (c) 2025, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import json, base64, re, frappe, hashlib, uuid
import xml.etree.ElementTree as ET
from erpnext_moldova_efactura.utils.fiscal_status import (
    determine_fiscal_status,
    is_sef_cancelable_status,
    is_sef_pending,
    sef_status_label,
    SEF_CANCELED_BY_SUPPLIER,
    SEF_PENDING_REGISTRATION,
    SEF_REGISTERED_AS_DRAFT,
)
from erpnext_moldova_efactura.utils.si_link import sales_invoice_of, sync_sales_invoice_links

from datetime import datetime
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import cint, flt
from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from erpnext_moldova_efactura.utils.api_response import invoice_status_map, sfs_action_error
from erpnext_moldova_efactura.utils.invoice_xml import unescape_sfs_text
from erpnext_moldova_efactura.utils.taxpayer_type import taxpayer_type_from_sfs, taxpayer_type_to_sfs
from erpnext_moldova_efactura.utils.timeline import log_event, log_status_change
from lxml import etree
from erpnext_moldova_efactura.utils.sef_mode import (
    expected_party_type,
    party_type as sef_party_type,
    resolve_xml_customer_party,
    sef_customer,
    throw_if_sef_party_idno_mismatch,
)


def _get_sales_efactura(name, ptype="write"):
    if not name:
        frappe.throw(_("Missing eFactura document name."))
    doc = frappe.get_doc("Sales eFactura", name)
    doc.check_permission(ptype)
    return doc


def _parse_names(names):
    if isinstance(names, str):
        names = frappe.parse_json(names)
    if not names:
        return []
    if not isinstance(names, (list, tuple)):
        names = [names]
    unique = []
    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _signable_skip_reason(row):
    if not row:
        return _("Not found")
    if cint(row.docstatus) != 1:
        return _("Not submitted")
    if not is_sef_pending(row.ef_status):
        return _("Not in Pending Registration")
    return None


def _assert_can_register_signed(ef):
    if cint(ef.docstatus) != 1:
        frappe.throw(_("Only submitted Sales eFactura documents can be registered."))
    if not is_sef_pending(ef.ef_status):
        frappe.throw(_("Sales eFactura can be registered only in Pending Registration status."))


class SaleseFactura(Document):
    @property
    def customer(self):
        """Linked Customer when party type is Customer (SI/SO/DN path)."""
        return sef_customer(self)

    @customer.setter
    def customer(self, value):
        if self.meta.has_field("customer_party"):
            if not self.get("customer_party_type"):
                self.customer_party_type = sef_party_type(self)
            if sef_party_type(self) == "Customer":
                self.customer_party = value
            return
        self.__dict__["customer"] = value

    def onload(self):
        if self.meta.has_field("customer_party_type") and not (self.get("customer_party_type") or "").strip():
            self.customer_party_type = expected_party_type(self)
        if self.docstatus == 0:
            self.update_items_available_qty()

    def validate(self):
        self.set_naming_series()
        self.set_ef_currency_from_settings()
        self.apply_ef_conversion_rate_rules()
        sync_sales_invoice_links(self)
        self._sync_is_return_from_sales_invoice()
        self._validate_unique_series_number()
        resolve_xml_customer_party(self)
        throw_if_sef_party_idno_mismatch(self)
        self._validate_sales_invoice_customer()
        self.update_items_available_qty()
        self.set_status(log=False)
        self.apply_vat()

    def _validate_sales_invoice_customer(self):
        if not self.sales_invoice:
            return
        if sef_party_type(self) != "Customer":
            frappe.throw(_("Sales Invoice can be linked only when Party Type is Customer"))
        if not sef_customer(self):
            frappe.throw(_("Select Customer first"))
        si_customer = frappe.db.get_value("Sales Invoice", self.sales_invoice, "customer")
        if si_customer and si_customer != sef_customer(self):
            frappe.throw(
                _("Sales Invoice {0} does not belong to Customer {1}").format(
                    self.sales_invoice, sef_customer(self)
                )
            )

    def _sync_is_return_from_sales_invoice(self):
        if not self.meta.has_field("is_return") or not self.sales_invoice:
            return
        self.is_return = cint(
            frappe.db.get_value("Sales Invoice", self.sales_invoice, "is_return") or 0
        )

    def set_naming_series(self):
        if not self.is_new():
            return
        self.naming_series = (
            "ACC-SEF-NT-.YYYY.-" if self.type == "Non-Transfer" else "ACC-SEF-.YYYY.-"
        )

    def before_save(self):
        from erpnext_moldova_efactura.utils.qty_guard import enforce_si_qty_on_draft_save

        if self.flags.get("from_efactura_sync") or self.flags.get("ignore_si_qty_guard"):
            return
        enforce_si_qty_on_draft_save(self)

    def before_submit(self):
        self._validate_ready_to_submit()
        from erpnext_moldova_efactura.utils.qty_guard import enforce_si_qty_on_submit

        if self.flags.get("from_efactura_sync") or self.flags.get("ignore_si_qty_guard"):
            return
        enforce_si_qty_on_submit(self)

    def _validate_ready_to_submit(self):
        if not (sef_customer(self) or self.get("customer_party") or self.get("customer")):
            frappe.throw(_("Party is required before submit"))
        if not self.company_bank_account:
            frappe.throw(_("Company Bank Account is required before submit"))
        if not self.issue_date:
            frappe.throw(_("Issue Date is required before submit"))
        if not self.delivery_date:
            frappe.throw(_("Delivery Date is required before submit"))
        if not self.items:
            frappe.throw(_("Add or fetch invoice items before submit"))

        from erpnext_moldova_efactura.utils.pi_match import throw_unmapped_items

        throw_if_sef_party_idno_mismatch(self)
        throw_unmapped_items(self.items, _("Map all items before submit"), self.currency)
        for row in self.items:
            if not row.uom:
                frappe.throw(_("UOM is required for row {0}").format(row.idx))
            if not row.stock_uom:
                frappe.throw(_("Stock UOM is required for row {0}").format(row.idx))
            if not row.ef_uom:
                frappe.throw(_("eFactura UOM is required for row {0}").format(row.idx))
            if not flt(row.qty):
                frappe.throw(_("Quantity is required for row {0}").format(row.idx))

    def on_submit(self):
        self.set_status(log=False)

    def on_cancel(self):
        if not is_sef_pending(self.ef_status) and self.ef_status != SEF_CANCELED_BY_SUPPLIER:
            frappe.throw(
                _("eFactura can be cancelled only in Pending Registration or Canceled by Supplier status.")
            )
        self.set_status(log=False)

    def on_update(self):
        # Auto-fill parties data after saving the document (draft included).
        # Use db_set(update_modified=False) to avoid recursive saves.
        self._autofill_parties_from_efactura_api_after_save()

    def save_version(self):
        from erpnext_moldova_efactura.utils.timeline import save_doc_version

        save_doc_version(self)

    def set_status(self, log=True):
        """Document Status (Draft/Submitted/Cancelled/Return) vs SFS eFactura Status text."""
        self.ef_status = sef_status_label(self.ef_status) or self.ef_status
        if self.docstatus == 1 and not self.ef_status:
            self.ef_status = SEF_PENDING_REGISTRATION
            if not self.is_new():
                self.db_set("ef_status", self.ef_status, update_modified=False)

        if self.docstatus == 0:
            self.status = "Draft"
            if self.is_new():
                return
        elif self.docstatus == 2:
            self.status = "Cancelled"
        elif cint(self.get("is_return")) == 1:
            self.status = "Return"
        else:
            self.status = "Submitted"

        if self.is_new():
            return

        old_ef = frappe.db.get_value(self.doctype, self.name, "ef_status")
        self.db_set(
            {"status": self.status, "ef_status": self.ef_status},
            update_modified=False,
        )
        if log:
            log_status_change(self, old_ef, self.ef_status)

        # --- Update linked Sales Invoice fiscal status ---
        si_name = sales_invoice_of(self)
        if si_name:
            try:
                si = frappe.get_doc("Sales Invoice", si_name)
                new_status = determine_fiscal_status(si)
                si.db_set("fiscal_status", new_status, update_modified=False)
            except frappe.ValidationError:
                # configuration error or blocked state – do not break eFactura flow
                pass

    def _validate_unique_series_number(self):
        if not (self.company and self.ef_series and self.ef_number):
            return
        existing = frappe.db.exists(
            "Sales eFactura",
            {
                "company": self.company,
                "ef_series": self.ef_series,
                "ef_number": self.ef_number,
                "name": ["!=", self.name],
            },
        )
        if existing:
            frappe.throw(
                _("Sales eFactura {0} already exists for {1}{2}").format(
                    existing, self.ef_series, self.ef_number
                )
            )

    def update_items_available_qty(self):
        si_name = sales_invoice_of(self)
        if not si_name:
            return

        current_qty = {}
        for item in self.items:
            if not item.item_code:
                continue
            current_qty[item.item_code] = current_qty.get(item.item_code, 0) + flt(item.stock_qty)

        from erpnext_moldova_efactura.utils.qty_guard import get_quota_efactura_names

        exclude_name = self.name if self.name and not self.is_new() else None
        efactura_names = get_quota_efactura_names(si_name, exclude_name=exclude_name)

        for item in self.items:
            if not item.item_code:
                continue
            total_si_stock_qty = (
                frappe.db.get_value(
                    "Sales Invoice Item",
                    {"parent": si_name, "item_code": item.item_code},
                    "sum(stock_qty)",
                )
                or 0
            )
            used_stock_qty = 0
            if efactura_names:
                used_stock_qty = (
                    frappe.db.get_value(
                        "Sales eFactura Item",
                        {"item_code": item.item_code, "parent": ["in", efactura_names]},
                        "sum(stock_qty)",
                    )
                    or 0
                )
            sibling_qty = current_qty.get(item.item_code, 0) - flt(item.stock_qty)
            item.available_stock_qty = flt(total_si_stock_qty) - flt(used_stock_qty) - flt(sibling_qty)

    def set_ef_currency_from_settings(self):
        ef_cur = frappe.db.get_single_value("eFactura Settings", "currency")
        if not ef_cur:
            frappe.throw(_("Please set Currency in eFactura Settings."))
        self.ef_currency = ef_cur

    def apply_ef_conversion_rate_rules(self):
        if not self.currency or not self.ef_currency:
            return

        if self.currency == self.ef_currency:
            self.ef_conversion_rate = 1
            return

        # If user did not set rate, try to fetch it using issue_date
        if not self.ef_conversion_rate or self.ef_conversion_rate <= 0:
            tx_date = self.issue_date or frappe.utils.today()

            from erpnext.setup.utils import get_exchange_rate

            rate = get_exchange_rate(self.currency, self.ef_currency, tx_date)

            if rate:
                self.ef_conversion_rate = rate

    def _keep_sfs_xml_amounts(self):
        """Invoices already issued in SFS keep XML money; do not recalc from tax templates."""
        if self.flags.get("keep_xml_amounts") or self.flags.get("from_efactura_sync"):
            return True
        try:
            status = sef_status_label(self.ef_status)
        except (TypeError, ValueError):
            return False
        return status != SEF_PENDING_REGISTRATION and bool(self.ef_series and self.ef_number)

    def _apply_sfs_xml_amounts(self):
        """Document amounts = eFactura XML amounts / (doc → ef rate)."""
        self.apply_ef_conversion_rate_rules()
        conv = flt(self.ef_conversion_rate) or 1
        vat_included = cint(
            frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate") or 0
        )
        self.net_total = flt(self.ef_net_total) / conv
        self.vat_total = flt(self.ef_vat_total) / conv
        self.total = flt(self.ef_total) / conv
        for d in self.items or []:
            d.net_amount = flt(d.ef_net_amount) / conv
            d.vat_amount = flt(d.ef_vat_amount) / conv
            d.net_rate = flt(d.ef_net_rate) / conv
            if vat_included:
                qty = flt(d.qty) or 1
                d.amount = d.net_amount + d.vat_amount
                d.rate = flt(d.ef_rate) / conv or (d.amount / qty)
            else:
                d.amount = d.net_amount
                d.rate = d.net_rate

    def apply_vat(self):
        if self._keep_sfs_xml_amounts():
            self._apply_sfs_xml_amounts()
            return

        vat_included = cint(
            frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate") or 0
        )
        ef_conv = flt(self.ef_conversion_rate) or 1


        tpl_cache = {}
        self.ef_vat_total = 0
        self.ef_net_total = 0
        self.ef_total = 0
        self.net_total = 0
        self.vat_total = 0
        self.total = 0

        for d in self.items or []:
            qty = flt(d.qty or 0)
            rate = flt(d.rate or 0)

            # Base amounts (document currency)
            amount = qty * rate
            d.amount = amount

            # Base ef amounts BEFORE VAT rule
            ef_rate = rate * ef_conv
            ef_amount = amount * ef_conv

            vat_rate = _get_vat_rate_from_item_tax_template(d.item_tax_template, tpl_cache)
            if not vat_rate:
                vat_rate = flt(d.ef_vat_rate)
            d.ef_vat_rate = vat_rate

            if not vat_rate:
                d.net_rate = rate
                d.net_amount = amount
                d.vat_amount = 0
                d.ef_net_rate = ef_rate
                d.ef_net_amount = ef_amount
                d.ef_vat_amount = 0
                d.ef_rate = ef_rate
                d.ef_amount = ef_amount
            elif vat_included:
                # rate includes VAT -> ef_amount is gross
                divider = 1 + vat_rate / 100
                net_amount = amount / divider if divider else amount
                d.net_rate = rate / divider if divider else rate
                d.net_amount = net_amount
                d.vat_amount = amount - net_amount

                ef_net_amount = ef_amount / divider if divider else ef_amount
                d.ef_net_amount = ef_net_amount
                d.ef_vat_amount = ef_amount - ef_net_amount
                d.ef_net_rate = ef_rate / divider if divider else ef_rate
                d.ef_rate = ef_rate
                d.ef_amount = ef_amount
            else:
                # rate excludes VAT -> ef_amount must become gross (your rule)
                vat_amount = amount * (vat_rate / 100)
                d.net_rate = rate
                d.vat_amount = vat_amount
                d.net_amount = amount

                ef_vat_amount = ef_amount * (vat_rate / 100)
                d.ef_net_amount = ef_amount
                d.ef_vat_amount = ef_vat_amount
                d.ef_net_rate = ef_rate

                d.ef_rate = ef_rate * (1 + vat_rate / 100)
                d.ef_amount = ef_amount + ef_vat_amount

            self.ef_vat_total += d.ef_vat_amount
            self.ef_net_total += d.ef_net_amount
            self.ef_total += d.ef_amount
            self.vat_total += d.vat_amount
            self.net_total += d.net_amount
            self.total += d.amount

    def fill_from_xml(self, xml_content: str, preserve_mapped_items: bool = True):
        """Load parties and items from an SFS invoice XML (outgoing invoices issued outside ERP)."""
        from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml
        from erpnext_moldova_efactura.utils.item_tax_template import item_tax_template_for_vat_rate
        from erpnext_moldova_efactura.utils.uom_map import ensure_uom_map, resolve_uom

        parsed = parse_invoice_xml(xml_content)
        if parsed.get("issue_date"):
            self.issue_date = parsed["issue_date"]
        if parsed.get("delivery_date"):
            self.delivery_date = parsed["delivery_date"]
        if parsed.get("ef_series"):
            self.ef_series = parsed["ef_series"]
        if parsed.get("ef_number"):
            self.ef_number = parsed["ef_number"]
        if parsed.get("creation_motiv") not in (None, ""):
            self.type = "Non-Transfer" if str(parsed["creation_motiv"]) == "5" else "Transfer"

        sup = parsed.get("supplier") or {}
        self.ef_supplier_idno = sup.get("idno") or self.ef_supplier_idno
        self.ef_supplier_name = sup.get("name") or self.ef_supplier_name
        self.ef_supplier_vat_id = sup.get("vat_id") or self.ef_supplier_vat_id
        self.ef_supplier_taxpayer_type = (
            sup.get("taxpayer_type") or self.ef_supplier_taxpayer_type
        )
        self.ef_supplier_address = sup.get("address") or self.ef_supplier_address
        self.ef_supplier_bank_account = sup.get("bank_account") or self.ef_supplier_bank_account
        self.ef_supplier_bank_name = sup.get("bank_name") or self.ef_supplier_bank_name
        self.ef_supplier_bank_code = sup.get("bank_code") or self.ef_supplier_bank_code

        buy = parsed.get("buyer") or {}
        self.ef_customer_idno = buy.get("idno") or self.ef_customer_idno
        self.ef_customer_name = buy.get("name") or self.ef_customer_name
        self.ef_customer_vat_id = buy.get("vat_id") or self.ef_customer_vat_id
        self.ef_customer_taxpayer_type = (
            buy.get("taxpayer_type") or self.ef_customer_taxpayer_type
        )
        self.ef_customer_address = buy.get("address") or self.ef_customer_address
        resolve_xml_customer_party(self)

        tr = parsed.get("transporter") or {}
        self.ef_transporter_idno = tr.get("idno") or self.ef_transporter_idno
        self.ef_transporter_name = tr.get("name") or self.ef_transporter_name
        self.ef_transporter_address = tr.get("address") or self.ef_transporter_address

        existing_maps = {}
        if preserve_mapped_items:
            for row in self.items or []:
                key = (row.ef_item_code or "").strip()
                if key and row.item_code:
                    existing_maps[key] = {
                        "item_code": row.item_code,
                        "uom": row.uom,
                        "ef_uom": row.ef_uom,
                        "conversion_factor": row.conversion_factor,
                        "ef_conversion_factor": row.ef_conversion_factor,
                        "item_tax_template": row.item_tax_template,
                    }

        fallback_uom = _fallback_uom()
        self.set("items", [])
        for item in parsed.get("items") or []:
            code = (item.get("supplier_item_code") or "").strip()
            prev = existing_maps.get(code) or {}
            item_code = prev.get("item_code") or (
                code if code and frappe.db.exists("Item", code) else None
            )
            resolved = prev.get("uom") or resolve_uom(item.get("supplier_uom"))
            uom = resolved or fallback_uom
            ef_uom = prev.get("ef_uom") or uom
            if item.get("supplier_uom") and resolved:
                ensure_uom_map(item.get("supplier_uom"), resolved)
            qty = flt(item.get("qty"))
            net_rate = flt(item.get("rate"))
            net_amount = flt(item.get("net_amount"))
            vat_amount = flt(item.get("vat_amount"))
            gross_amount = flt(item.get("amount"))
            vat_rate = flt(item.get("ef_vat_rate"))
            item_tax_template = prev.get("item_tax_template") or item_tax_template_for_vat_rate(
                vat_rate, self.company
            )
            self.append(
                "items",
                {
                    "item_code": item_code,
                    "ef_item_code": code,
                    "item_name": (item.get("supplier_item_name") or code or "Item")[:140],
                    "qty": qty,
                    "uom": uom,
                    "ef_uom": ef_uom,
                    "ef_qty": flt(item.get("ef_qty") or qty),
                    "stock_uom": uom,
                    "stock_qty": qty,
                    "conversion_factor": flt(prev.get("conversion_factor") or 1),
                    "ef_conversion_factor": flt(prev.get("ef_conversion_factor") or 1),
                    "item_tax_template": item_tax_template,
                    "rate": net_rate,
                    "amount": net_amount,
                    "net_rate": net_rate,
                    "net_amount": net_amount,
                    "vat_amount": vat_amount,
                    "ef_rate": flt(item.get("rate_with_vat") or net_rate),
                    "ef_amount": gross_amount,
                    "ef_net_rate": net_rate,
                    "ef_net_amount": net_amount,
                    "ef_vat_amount": vat_amount,
                    "ef_vat_rate": vat_rate,
                },
            )

        self.ef_total = flt(parsed.get("total"))
        self.ef_vat_total = flt(parsed.get("vat_total"))
        self.ef_net_total = flt(parsed.get("net_total"))
        self.flags.keep_xml_amounts = True
        self.set_ef_currency_from_settings()
        self.apply_ef_conversion_rate_rules()
        self._apply_sfs_xml_amounts()

    def _autofill_parties_from_efactura_api_after_save(self):
        # Prevent recursion
        if getattr(self.flags, "ef_autofill_running", False):
            return
        if self.flags.get("from_efactura_sync"):
            return

        # Do not run on cancel
        if self.docstatus == 2:
            return

        idno_fields = {}

        idno_fields['Company'] = frappe.db.get_single_value(
            "eFactura Settings", "company_idno_field"
        )
        if not idno_fields['Company']:
            return

        idno_fields["Supplier"] = frappe.db.get_single_value(
            "eFactura Settings", "supplier_idno_field"
        )
        if not idno_fields["Supplier"]:
            return

        idno_fields["Customer"] = frappe.db.get_single_value(
            "eFactura Settings", "customer_idno_field"
        )
        if not idno_fields["Customer"]:
            return

        self.flags.ef_autofill_running = True
        try:
            from erpnext_moldova_efactura.api_client import EFacturaAPIClient

            client = EFacturaAPIClient.from_settings(company=self.company)

            self._autofill_party_block(
                client,
                "supplier",
                "Company",
                self.company,
                idno_fields["Company"],
            )
            ptype = sef_party_type(self)
            self._autofill_party_block(
                client,
                "customer",
                ptype,
                sef_customer(self) or self.get("customer_party") or self.get("customer"),
                idno_fields.get(ptype),
            )

            if self.transporter_party_type and self.transporter_party:
                self._autofill_party_block(
                    client,
                    "transporter",
                    self.transporter_party_type,
                    self.transporter_party,
                    idno_fields[self.transporter_party_type],
                )
            else:
                self._clear_party_block("transporter")

        except Exception:
            # Do not block saving in draft; log for diagnostics.
            frappe.log_error(frappe.get_traceback(), "eFactura: autofill parties failed")
        finally:
            self.flags.ef_autofill_running = False


    def _clear_party_block(self, prefix):
        self.db_set(f"ef_{prefix}_idno", "", update_modified=False)
        self.db_set(f"ef_{prefix}_vat_id", "", update_modified=False)
        self.db_set(f"ef_{prefix}_name", "", update_modified=False)
        self.db_set(f"ef_{prefix}_address", "", update_modified=False)
        self.db_set(f"ef_{prefix}_taxpayer_type", "", update_modified=False)
        self.db_set(f"ef_{prefix}_is_user", "", update_modified=False)
        self.db_set(f"ef_{prefix}_bank_account", "", update_modified=False)
        self.db_set(f"ef_{prefix}_bank_name", "", update_modified=False)
        self.db_set(f"ef_{prefix}_bank_code", "", update_modified=False)


    def _autofill_party_block(self, client, prefix, party_doctype, party_name, idno_fieldname):
        if not party_doctype or not party_name or not idno_fieldname:
            return

        meta = frappe.get_meta(party_doctype)
        if not meta.has_field(idno_fieldname):
            return

        party_idno = frappe.db.get_value(party_doctype, party_name, idno_fieldname)
        if not party_idno:
            return

        # If IDNO already filled and equal to party IDNO do not overwrite
        idno_value = getattr(self, f"ef_{prefix}_idno", None)

        if not idno_value or party_idno != idno_value:
            # 1) GetTaxpayersInfo
            tax_resp = client.get_taxpayers_info([party_idno])
            taxpayers = (tax_resp.get("Results") or {}).get("Taxpayer") or []
            taxpayer = taxpayers[0] if taxpayers else {}

            idno = taxpayer.get("IDNO") or ""
            vat_id = taxpayer.get("CodTVA") or ""
            name = taxpayer.get("Name") or ""
            address = taxpayer.get("Address") or ""
            taxpayer_type = taxpayer_type_from_sfs(taxpayer.get("TaxpayerType") or "")
            is_user = "Yes" if taxpayer.get("IsEFacturaActor") else "No"

            self.db_set(f"ef_{prefix}_idno", idno, update_modified=False)
            self.db_set(f"ef_{prefix}_vat_id", vat_id, update_modified=False)
            self.db_set(f"ef_{prefix}_name", name, update_modified=False)
            self.db_set(f"ef_{prefix}_address", address, update_modified=False)
            self.db_set(f"ef_{prefix}_taxpayer_type", taxpayer_type, update_modified=False)
            self.db_set(f"ef_{prefix}_is_user", is_user, update_modified=False)

        # 2) GetBankAccountInfo when the form has a Bank Account link
        # (supplier uses company_bank_account after the v2 rename).
        ba_field = _party_bank_link_field(prefix)
        if ba_field not in self.get_valid_columns():
            return

        ba_name = getattr(self, ba_field, None) or ""
        if not ba_name:
            return

        bank_account, bank_name, bank_code = _local_bank_details(ba_name)
        if not bank_account:
            return

        current_account = getattr(self, f"ef_{prefix}_bank_account", None) or ""
        if bank_account and (
            bank_account != current_account
            or not getattr(self, f"ef_{prefix}_bank_name", None)
            or not getattr(self, f"ef_{prefix}_bank_code", None)
        ):
            try:
                bank_resp = client.get_bank_account_info(
                    idno=party_idno, account_number=bank_account
                )
                for bank in (bank_resp.get("Results") or {}).get("BankAccount") or []:
                    if bank.get("AccountNumber") == bank_account:
                        bank_name = unescape_sfs_text(bank.get("BranchTitle") or "") or bank_name
                        bank_code = bank.get("BranchCode") or bank_code
                        break
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(), "eFactura: GetBankAccountInfo failed"
                )

        self.db_set(f"ef_{prefix}_bank_account", bank_account, update_modified=False)
        self.db_set(f"ef_{prefix}_bank_name", bank_name, update_modified=False)
        self.db_set(f"ef_{prefix}_bank_code", bank_code, update_modified=False)

@frappe.whitelist()
def download_xml(efactura_name):
    efactura = _get_sales_efactura(efactura_name, "read")
    ef_lang = frappe.db.get_single_value("eFactura Settings", "language")

    xml_content = _generate_invoice_xml(
        efactura=efactura,
        language=ef_lang,
    )

    frappe.local.response.filename = f"{efactura.name}.xml"
    frappe.local.response.filecontent = xml_content
    frappe.local.response.type = "download"
    frappe.local.response.content_type = "application/xml"


@frappe.whitelist()
def update_ef_status(efactura_name):
    efactura = _get_sales_efactura(efactura_name)
    client = EFacturaAPIClient.from_settings(company=efactura.company)

    if not efactura.ef_series or not efactura.ef_number:
        # List of statuses to check in sequence (eFactura API requires status filter)
        search_statuses = [0,1,7,8,3,2,5,10,4,6,9]
        for status in search_statuses:
            params = {
                "APIeInvoiceId": efactura.name, 
                "InvoiceStatus": status,
            }

            resp = client.search_invoices(actor_role=1, parameters=params)
            inv = _extract_single_invoice_from_search_response(resp)
            
            if inv:
                break

        if isinstance(inv, list):
            frappe.throw(_("e-Factura returned multiple invoices for APIeInvoiceId={0}: {1}").format(efactura.name, len(inv)))

        if isinstance(inv, dict):
            remote_series = (inv.get("Seria") or "").strip()
            remote_number = (inv.get("Number") or "").strip()
            remote_status = inv.get("InvoiceStatus")
        
            if remote_series and remote_number and remote_status is not None:
                assigned = not (efactura.ef_series and efactura.ef_number)
                efactura.db_set("ef_series", remote_series, update_modified=False)
                efactura.db_set("ef_number", remote_number, update_modified=False)
                efactura.db_set("ef_status", sef_status_label(remote_status), update_modified=False)
                efactura.set_status()
                if assigned:
                    log_event(
                        efactura,
                        _("Assigned series and number {0}{1} from e-Factura.").format(
                            remote_series, remote_number
                        ),
                    ) 

    else:
        resp = client.check_invoices_status(seria_and_numbers=
            [
                {
                    "Seria": efactura.ef_series,
                    "Number": efactura.ef_number,
                }
            ]
        )

        statuses = _extract_status_map(resp)

        key = (str(efactura.ef_series), str(efactura.ef_number))
        status = statuses.get(key)

        if status is not None and sef_status_label(status) != efactura.ef_status:
            efactura.db_set("ef_status", sef_status_label(status), update_modified=False)
            efactura.set_status()


def _assert_can_cancel(doc):
    if cint(doc.docstatus) != 1:
        frappe.throw(_("Submit Sales eFactura before this action"))
    if not doc.ef_series or not doc.ef_number:
        frappe.throw(_("eFactura Series/Number is required to cancel in e-Factura"))
    if not is_sef_cancelable_status(doc.ef_status):
        frappe.throw(
            _(
                "eFactura can be canceled only in Signed by Supplier, Sent to Customer, "
                "Accepted by Customer, Signed by Customer, Rejected by Customer, or Transportation status."
            )
        )


def _refresh_sfs_status(doc):
    client = EFacturaAPIClient.from_settings(company=doc.company)
    resp = client.check_invoices_status([{"Seria": doc.ef_series, "Number": doc.ef_number}])
    statuses = invoice_status_map(resp)
    status = statuses.get((str(doc.ef_series), str(doc.ef_number)))
    if status is not None and sef_status_label(status) != doc.ef_status:
        label = sef_status_label(status)
        doc.db_set("ef_status", label, update_modified=False)
        doc.ef_status = label
        doc.set_status()


@frappe.whitelist()
def cancel_invoice(name: str, reason: str | None = None):
    """Cancel (anulare) a Sales eFactura in SFS, same pattern as Purchase eFactura reject."""
    doc = _get_sales_efactura(name)
    _assert_can_cancel(doc)
    comment = (reason or doc.get("cancellation_reason") or "").strip()
    if not comment:
        frappe.throw(_("Cancellation Reason is required"))

    client = EFacturaAPIClient.from_settings(company=doc.company)
    try:
        resp = client.post_canceled_invoices(
            [
                {
                    "Seria": doc.ef_series,
                    "Number": doc.ef_number,
                    "Comment": comment,
                }
            ]
        )
    except Exception as e:
        frappe.throw(_("e-Factura API Error: {0}").format(str(e)))
    err = sfs_action_error(resp)
    if err:
        frappe.throw(_("e-Factura API Error: {0}").format(err))

    doc.db_set("cancellation_reason", comment, update_modified=False)
    doc.cancellation_reason = comment
    _refresh_sfs_status(doc)
    log_event(doc, _("Canceled invoice in e-Factura: {0}").format(comment))
    return {"status": doc.status, "ef_status": doc.ef_status, "cancellation_reason": comment}


@frappe.whitelist()
def download_pdf(efactura_name):
    efactura = _get_sales_efactura(efactura_name, "read")

    client = EFacturaAPIClient.from_settings(company=efactura.company)
    resp = client.get_invoices_content_for_print(seria_and_numbers=
        {
            "Seria": efactura.ef_series,
            "Number": efactura.ef_number,
        },
        actor_role=1
    )

    pdf_content = (resp or {}).get("Result", {}).get("Content") or ""

    # sanity check
    if not pdf_content.startswith(b"%PDF"):
        frappe.throw(_("e-Factura returned non-PDF content in Result.Content"))

    filename = f"{efactura.ef_series}{efactura.ef_number}.pdf"

    frappe.local.response.filename = filename
    frappe.local.response.filecontent = pdf_content
    frappe.local.response.type = "download"
    frappe.local.response.content_type = "application/pdf"


@frappe.whitelist()
def filter_signable(names=None):
    """Return selected Sales eFactura names that can be signed (submitted, pending)."""
    names = _parse_names(names)
    if not names:
        return {"signable": [], "skipped": []}

    rows = frappe.get_all(
        "Sales eFactura",
        filters={"name": ["in", names]},
        fields=["name", "docstatus", "ef_status", "status"],
    )
    by_name = {row.name: row for row in rows}
    signable = []
    skipped = []
    for name in names:
        row = by_name.get(name)
        reason = _signable_skip_reason(row)
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        if not frappe.has_permission("Sales eFactura", "write", name):
            skipped.append({"name": name, "reason": _("No write permission")})
            continue
        signable.append({"name": name, "status": row.ef_status or row.status or ""})
    return {"signable": signable, "skipped": skipped}


@frappe.whitelist()
def get_for_sign(efactura_name):
    efactura = _get_sales_efactura(efactura_name)
    _assert_can_register_signed(efactura)
    ef_lang = frappe.db.get_single_value("eFactura Settings", "language")

    if not efactura.ef_series or not efactura.ef_number:
        client = EFacturaAPIClient.from_settings(company=efactura.company)
        resp = client.get_series_and_numbers(count=1)
        data = resp.get("Results", {}).get("SeriaAndNumber", [{}])[0]

        efactura.db_set("ef_series", data.get("Seria"))
        efactura.db_set("ef_number", data.get("Number"))

        if not efactura.ef_series or not efactura.ef_number:
            frappe.throw(_("e-Factura API Error: Unable to obtain Series and Number"))

        log_event(
            efactura,
            _("Assigned series and number {0}{1} for signing.").format(
                efactura.ef_series, efactura.ef_number
            ),
        )

    xml_content = _generate_invoice_xml(
        efactura=efactura,
        language=ef_lang,
        document=False,
        declaration=False
    )
    
    def calculate_hash(xml_bytes: bytes) -> bytes:
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser)

        can = etree.tostring(
            root,
            method="c14n",
            exclusive=False,
            with_comments=False
        )
        return hashlib.sha1(can).digest() 

    hash = calculate_hash(xml_content)

    return {
        "xml_base64": base64.b64encode(xml_content).decode('utf-8'),
        "hash_base64": base64.b64encode(hash).decode('utf-8'),
    }

@frappe.whitelist()
def send_unsigned(efactura_name):
    efactura = _get_sales_efactura(efactura_name)
    _assert_can_register_signed(efactura)
    ef_lang = frappe.db.get_single_value("eFactura Settings", "language")

    client = EFacturaAPIClient.from_settings(company=efactura.company)

    xml_content = _generate_invoice_xml(
        efactura=efactura,
        language=ef_lang,
    )

    resp = client.post_invoices(
        request_id=efactura.name, actor_role=1, invoices_xml=xml_content, invoices_xml_status=0
    )

    error_message = resp.get("ErrorMessage")
    total = resp.get("TotalInvoices", 0)
    posted = resp.get("TotalInvoicesPosted", 0)

    if error_message:
        frappe.throw(_("e-Factura API Error: {0}").format(error_message))

    elif total != posted or posted == 0:
        frappe.throw(_("e-Factura API Error: Invoices posted: {0} / {1}").format(posted, total))

    else:
        efactura.db_set("ef_status", SEF_REGISTERED_AS_DRAFT, update_modified=False)
        efactura.set_status()
        # series and number are assigned only after signing in eFactura system, 
        # so we need to clear them for unsigned invoices to avoid confusion
        efactura.db_set("ef_series", None, update_modified=False)
        efactura.db_set("ef_number", None, update_modified=False)
        log_event(efactura, _("Sent unsigned invoice to e-Factura (draft)."))
        return {
            "message": _("Successfully sent {0} unsigned invoice(s) to e-Factura system.").format(
                posted
        )}


@frappe.whitelist()
def update_dates(efactura_name, issue_date, delivery_date):
    """Update issue_date and delivery_date for submitted eFactura in Pending status."""
    ef = _get_sales_efactura(efactura_name)

    if ef.docstatus != 1:
        frappe.throw(_("Dates can be updated only for submitted documents."))

    if not is_sef_pending(ef.ef_status):
        frappe.throw(_("Dates can be updated only in Pending Registration status."))

    if not issue_date or not delivery_date:
        frappe.throw(_("Both Issue Date and Delivery Date are required."))

    # Normalize to YYYY-MM-DD
    issue_date = frappe.utils.getdate(issue_date)
    delivery_date = frappe.utils.getdate(delivery_date)

    old_issue = ef.issue_date
    old_delivery = ef.delivery_date
    ef.db_set("issue_date", issue_date, update_modified=False)
    ef.db_set("delivery_date", delivery_date, update_modified=False)
    log_event(
        ef,
        _("Issue Date / Delivery Date updated: {0} / {1} → {2} / {3}").format(
            old_issue or "—", old_delivery or "—", issue_date, delivery_date
        ),
    )

    return {
        "issue_date": str(issue_date),
        "delivery_date": str(delivery_date),
    }


@frappe.whitelist()
def process_signed_xml(name, signature, content):

    if not name:
        frappe.throw(_("Missing eFactura document name."))

    if not signature:
        frappe.throw(_("Missing signature."))

    if not content:
        frappe.throw(_("Missing content."))

    def _b64_to_text(b64_value: str) -> str:
        try:
            raw = base64.b64decode(b64_value)
        except Exception:
            frappe.throw(_("Invalid base64 payload."))

        # Strip UTF-8 BOM if present
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]

        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="replace")

    def _strip_xml_declaration(xml_text: str) -> str:
        # Remove any leading XML declaration like:
        # <?xml version="1.0" encoding="UTF-8" standalone="no"?>
        if not xml_text:
            return ""
        s = xml_text.lstrip()
        s = re.sub(r"^<\?xml[^>]*\?>\s*", "", s, flags=re.IGNORECASE)
        return s.strip()

    content_xml = _strip_xml_declaration(_b64_to_text(content))
    signature_xml = _strip_xml_declaration(_b64_to_text(signature))
    
    # Compose final XML without altering inner whitespace/formatting.
    final_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<Documents>\n'
        '<Document>\n'
        f'{content_xml}\n'
        '<Signatures>\n'
        '<SignatureContent>\n'
        '<SignedDoc>\n'
        f'<hash Id="_{uuid.uuid4()}">Hash is incapsulated into the signature</hash>\n'
        f'{signature_xml}\n'
        '</SignedDoc>\n'
        '</SignatureContent>\n'
        '</Signatures>\n'
        '</Document>\n'
        '</Documents>\n'
    )

    ef = _get_sales_efactura(name)
    _assert_can_register_signed(ef)

    # Send signed XML via PostInvoices
    client = EFacturaAPIClient.from_settings(company=ef.company)

    # NOTE:
    # - send_unsigned() uses invoices_xml_status=0 (unsigned)
    # - signed XML should use invoices_xml_status=1
    try:
        resp = client.post_invoices(
            request_id=ef.name,
            actor_role=1,
            invoices_xml=final_xml,
            invoices_xml_status=1,
        )
    except Exception as e:
        frappe.throw(_("e-Factura API Error: {0}").format(str(e)))

    error_message = (resp or {}).get("ErrorMessage")
    total = (resp or {}).get("TotalInvoices", 0) or 0
    posted = (resp or {}).get("TotalInvoicesPosted", 0) or 0

    if error_message:
        frappe.throw(_("e-Factura API Error: {0}").format(error_message))

    if total != posted or posted == 0:
        frappe.throw(_("e-Factura API Error: Invoices posted: {0} / {1}").format(posted, total))

    # Update status
    ef.db_set("ef_status", sef_status_label(1), update_modified=False)
    ef.set_status()
    log_event(ef, _("Sent signed invoice to e-Factura."))

    return {
        "message": _("Successfully sent {0} signed invoice(s) to e-Factura system.").format(posted),
        "total": total,
        "posted": posted,
    }


@frappe.whitelist()
def get_new_customer_defaults(name=None, title=None, idno=None, taxpayer_type=None):
    """Prefill values when creating a Customer from a Sales eFactura loaded from SFS."""
    if name:
        doc = _get_sales_efactura(name, "read")
        title = title or doc.ef_customer_name
        idno = idno or doc.ef_customer_idno
        taxpayer_type = taxpayer_type or doc.ef_customer_taxpayer_type
    from erpnext_moldova_efactura.utils.party import new_customer_defaults

    return new_customer_defaults(title, idno, taxpayer_type)


@frappe.whitelist()
def get_new_supplier_defaults(name=None, title=None, idno=None):
    """Prefill values when creating a Supplier from a Non-Transfer Sales eFactura."""
    if name:
        doc = _get_sales_efactura(name, "read")
        title = title or doc.ef_customer_name
        idno = idno or doc.ef_customer_idno
    from erpnext_moldova_efactura.utils.party import new_supplier_defaults

    return new_supplier_defaults(title, idno)


def _require_not_cancelled(doc):
    if cint(doc.docstatus) == 2:
        frappe.throw(_("Cannot create documents from a cancelled Sales eFactura"))


def _require_mapped(doc, action_label=None):
    if sef_party_type(doc) != "Customer" or not sef_customer(doc):
        frappe.throw(_("Customer is required to create {0}").format(action_label or _("Sales Invoice")))
    if not doc.items:
        frappe.throw(_("No items on Sales eFactura — fetch details first"))
    from erpnext_moldova_efactura.utils.party import throw_if_customer_idno_mismatch
    from erpnext_moldova_efactura.utils.pi_match import throw_unmapped_items

    throw_if_customer_idno_mismatch(sef_customer(doc), doc.ef_customer_idno)
    throw_unmapped_items(
        doc.items,
        _("Map all items before creating {0}").format(action_label or _("Sales Invoice")),
        doc.currency,
    )
    for row in doc.items:
        if not row.uom:
            frappe.throw(_("UOM is required for row {0}").format(row.idx))
        if not flt(row.qty):
            frappe.throw(_("Quantity is required for row {0}").format(row.idx))


def _selling_line_from_sef(row, vat_included: bool) -> dict:
    from erpnext_moldova_efactura.utils.uom_map import get_item_uom_conversion

    uom_cf = flt(row.conversion_factor) or get_item_uom_conversion(row.item_code, row.uom) or 1
    if vat_included:
        amount = flt(row.net_amount) + flt(row.vat_amount)
        if not amount:
            amount = flt(row.amount)
        rate = (amount / flt(row.qty)) if flt(row.qty) and amount else flt(row.rate)
    else:
        amount = flt(row.net_amount) or flt(row.amount)
        rate = (amount / flt(row.qty)) if flt(row.qty) and amount else flt(row.net_rate or row.rate)
    uom = row.uom if row.uom and frappe.db.exists("UOM", row.uom) else None
    return {
        "item_code": row.item_code,
        "item_name": row.item_name,
        "qty": row.qty,
        "uom": uom,
        "conversion_factor": uom_cf,
        "rate": rate,
        "amount": amount,
        "stock_uom": frappe.db.get_value("Item", row.item_code, "stock_uom"),
        "item_tax_template": getattr(row, "item_tax_template", None),
    }


def _append_selling_items(target, source):
    from erpnext_moldova_efactura.utils.buying_rate import BUYING_RATE_PRECISION
    from erpnext_moldova_efactura.utils.buying_taxes import apply_selling_taxes

    if target.meta.has_field("ignore_pricing_rule"):
        target.ignore_pricing_rule = 1
    vat_included = bool(frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate"))
    schedule = source.delivery_date or source.issue_date
    for row in source.items:
        vals = _selling_line_from_sef(row, vat_included)
        item = target.append("items", {})
        item.item_code = vals["item_code"]
        item.item_name = vals["item_name"]
        item.qty = vals["qty"]
        if vals["uom"]:
            item.uom = vals["uom"]
        item.conversion_factor = vals["conversion_factor"]
        item.rate = flt(vals["rate"], BUYING_RATE_PRECISION)
        if item.meta.has_field("price_list_rate"):
            item.price_list_rate = item.rate
        if item.meta.has_field("amount") and vals.get("amount"):
            item.amount = flt(vals["amount"], item.precision("amount") or 2)
        if vals["stock_uom"] and item.meta.has_field("stock_uom"):
            item.stock_uom = vals["stock_uom"]
        if vals.get("item_tax_template") and item.meta.has_field("item_tax_template"):
            item.item_tax_template = vals["item_tax_template"]
        if schedule and item.meta.has_field("delivery_date"):
            item.delivery_date = schedule
    apply_selling_taxes(target, source)


def _posting_time_str(value):
    if value in (None, ""):
        return None
    from frappe.utils import get_time

    try:
        t = get_time(value).replace(microsecond=0)
    except Exception:
        return None
    if t.hour == 0 and t.minute == 0 and t.second == 0:
        return None
    return t.strftime("%H:%M:%S")


def _apply_posting_from_factura(target, source):
    if not cint(frappe.db.get_single_value("eFactura Settings", "copy_date_from_factura")):
        return
    if not source.issue_date:
        return
    if target.meta.has_field("set_posting_time"):
        target.set_posting_time = 1
    if target.meta.has_field("posting_date"):
        target.posting_date = source.issue_date
    posting_time = _posting_time_str(getattr(source, "issue_time", None) or getattr(source, "posting_time", None))
    if target.meta.has_field("posting_time") and posting_time:
        target.posting_time = posting_time
    if target.meta.has_field("transaction_date"):
        target.transaction_date = source.issue_date


def _prepare_mapped_selling_doc(target):
    currency = target.currency or "MDL"
    if target.meta.has_field("price_list_currency") and not target.price_list_currency:
        target.price_list_currency = currency
    if target.meta.has_field("conversion_rate"):
        company_cur = (
            frappe.get_cached_value("Company", target.company, "default_currency")
            if target.company
            else None
        )
        need_rate = not flt(target.conversion_rate) or (
            flt(target.conversion_rate) == 1
            and currency
            and company_cur
            and currency != company_cur
        )
        if need_rate:
            if currency and company_cur and currency != company_cur:
                from erpnext.setup.utils import get_exchange_rate

                date = (
                    getattr(target, "posting_date", None)
                    or getattr(target, "transaction_date", None)
                    or frappe.utils.today()
                )
                target.conversion_rate = flt(get_exchange_rate(currency, company_cur, date)) or 1
            else:
                target.conversion_rate = 1
    if target.meta.has_field("plc_conversion_rate") and not flt(target.plc_conversion_rate):
        target.plc_conversion_rate = 1
    if target.meta.has_field("selling_price_list") and not target.selling_price_list:
        pl = frappe.db.get_single_value("Selling Settings", "selling_price_list")
        if pl:
            target.selling_price_list = pl
    target.set_onload("load_after_mapping", True)


def _set_sales_efactura_link(target, source_name):
    if target.meta.has_field("sales_efactura"):
        target.sales_efactura = source_name


def link_created_sales_invoice(si):
    """Copy the new Sales Invoice onto the source Sales eFactura."""
    name = si.get("sales_efactura") if si.meta.has_field("sales_efactura") else None
    if not name or not frappe.db.exists("Sales eFactura", name):
        return
    sef = frappe.get_doc("Sales eFactura", name)
    if sef.sales_invoice and sef.sales_invoice != si.name:
        return
    sef.sales_invoice = si.name
    for row, si_row in zip(sef.items or [], si.items or []):
        row.sales_invoice = si.name
        if si_row.name:
            row.si_detail = si_row.name
    sync_sales_invoice_links(sef)
    sef.flags.ignore_si_qty_guard = True
    if cint(sef.docstatus) == 1:
        sef.flags.ignore_validate_update_after_submit = True
    sef.save(ignore_permissions=True)


def unlink_created_sales_invoice(si):
    name = si.get("sales_efactura") if si.meta.has_field("sales_efactura") else None
    if not name or not frappe.db.exists("Sales eFactura", name):
        return
    sef = frappe.get_doc("Sales eFactura", name)
    if sef.sales_invoice != si.name:
        return
    sef.sales_invoice = None
    for row in sef.items or []:
        if row.sales_invoice == si.name:
            row.sales_invoice = None
            row.si_detail = None
    sef.flags.ignore_si_qty_guard = True
    if cint(sef.docstatus) == 1:
        sef.flags.ignore_validate_update_after_submit = True
    sef.save(ignore_permissions=True)


@frappe.whitelist()
def make_sales_invoice(source_name, target_doc=None):
    frappe.has_permission("Sales Invoice", "create", throw=True)
    source = _get_sales_efactura(source_name)
    _require_not_cancelled(source)
    _require_mapped(source, _("Sales Invoice"))
    if source.sales_invoice:
        frappe.throw(_("e-Factura already has a Sales Invoice"))

    si = frappe.new_doc("Sales Invoice")
    si.company = source.company
    si.customer = sef_customer(source)
    si.currency = source.currency or "MDL"
    if source.delivery_date and si.meta.has_field("delivery_date"):
        si.delivery_date = source.delivery_date
    _apply_posting_from_factura(si, source)
    _append_selling_items(si, source)
    _set_sales_efactura_link(si, source.name)
    _prepare_mapped_selling_doc(si)
    return si


@frappe.whitelist()
def make_sales_order(source_name, target_doc=None):
    from frappe.utils import today

    frappe.has_permission("Sales Order", "create", throw=True)
    source = _get_sales_efactura(source_name)
    _require_not_cancelled(source)
    _require_mapped(source, _("Sales Order"))

    so = frappe.new_doc("Sales Order")
    so.company = source.company
    so.customer = sef_customer(source)
    so.currency = source.currency or "MDL"
    so.transaction_date = today()
    schedule = source.delivery_date or source.issue_date or today()
    if so.meta.has_field("delivery_date"):
        so.delivery_date = schedule
    _apply_posting_from_factura(so, source)
    _append_selling_items(so, source)
    _set_sales_efactura_link(so, source.name)
    _prepare_mapped_selling_doc(so)
    return so


@frappe.whitelist()
def make_efactura_from_delivery_note(source_name, target_doc=None, args=None):
    if args is None:
        args = {}
    if isinstance(args, str):
        args = json.loads(args)

    doc = get_mapped_doc(
        "Delivery Note",
        source_name,
        {
            "Delivery Note": {
                "doctype": "Sales eFactura",
                "validation": {
                    "docstatus": ["=", 1]
                },
            },
            "Delivery Note Item": {
                "doctype": "Sales eFactura Item",
                "field_map": {
                    "item_code": "item_code",
                    "item_name": "item_name",
                    "ef_uom": "stock_uom",
                    "ef_qty": "stock_qty",
                    "ef_rate": "stock_uom_rate",
                    "stock_qty": "stock_qty",
                    "stock_uom": "stock_uom",
                    "uom": "uom",
                    "qty": "qty",
                    "rate": "rate",
                    "item_tax_template": "item_tax_template",
                    "parent": "delivery_note",
                    "name": "dn_detail",
                    "against_sales_invoice": "sales_invoice",
                    "sales_invoice": "sales_invoice",
                    "si_detail": "si_detail",
        },},},
        target_doc,
        _postprocess_with_discount,
    )

    if target_doc:
        target_doc.update_items_available_qty()

    doc.update_items_available_qty()

    return doc


@frappe.whitelist()
def make_efactura_from_sales_invoice(source_name, target_doc=None):

    doc = get_mapped_doc(
        "Sales Invoice",
        source_name,
        {
            "Sales Invoice": {
                "doctype": "Sales eFactura",
                "validation": {
                    "docstatus": ["=", 1]
                },
            },
            "Sales Invoice Item": {
                "doctype": "Sales eFactura Item",
                "field_map": {
                    "item_code": "item_code",
                    "item_name": "item_name",
                    "ef_uom": "stock_uom",
                    "ef_qty": "stock_qty",
                    "ef_rate": "stock_uom_rate",
                    "stock_qty": "stock_qty",
                    "stock_uom": "stock_uom",
                    "uom": "uom",
                    "qty": "qty",
                    "rate": "rate",
                    "item_tax_template": "item_tax_template",
                    "parent": "sales_invoice",
                    "name": "si_detail",
                    "delivery_note": "delivery_note",
                    "dn_detail": "dn_detail",
        },},},
        target_doc,
        _postprocess_with_discount,
    )
    if target_doc:
        target_doc.update_items_available_qty()

    doc.update_items_available_qty()

    return doc


def _postprocess_with_discount(source, target):
    _set_missing_values(source, target)
    _apply_additional_discounts(source, target)


def _apply_additional_discounts(source, target):
    
    apply_discount_on = source.get("apply_discount_on") # "Net Total" or "Grand Total"
    discount_amount = flt(source.get("discount_amount", 0))

    if discount_amount <= 0:
        return

    if apply_discount_on == "Net Total":
        base_amount = source.get("base_net_total", 0) + discount_amount
    else:       
        base_amount = source.get("base_total", 0)
    
    discount_percentage = (discount_amount / base_amount * 100) if base_amount else 0

    for d in target.items or []:        
        discount = discount_percentage * flt(d.amount or 0) / 100
        d.rate = flt(d.rate or 0) - (discount / flt(d.qty or 1))
        d.amount = flt(d.rate) * flt(d.qty or 0)

        ef_discount = discount_percentage * flt(d.ef_amount or 0) / 100 
        d.ef_rate = flt(d.ef_rate or 0) - (ef_discount / flt(d.ef_qty or 1)) 
        d.ef_amount = flt(d.ef_rate) * flt(d.ef_qty or 0)

    target.apply_vat()  # recalculate VAT after discount
    

def _resolve_sales_invoice_from_delivery_note(source):
    """Pick Sales Invoice linked to a Delivery Note (staged delivery against SI)."""
    for d in source.get("items") or []:
        si = d.get("against_sales_invoice") or d.get("sales_invoice")
        if si:
            return si
    return None


def _set_missing_values(source, target):
    # Parent fields
    target.company = source.company
    target.currency = source.currency

    target.customer_party_type = "Customer"
    target.customer_party = source.customer

    # One Sales Invoice per e-Factura (header is denormalized from item links):
    # - eFactura can be issued without any Delivery Note
    # - one SI may be fulfilled by several DNs; DN only supplies items
    if source.doctype == "Sales Invoice":
        target.sales_invoice = source.name
        if target.meta.has_field("is_return"):
            target.is_return = cint(source.is_return)
    elif source.doctype == "Delivery Note" and not target.sales_invoice:
        target.sales_invoice = _resolve_sales_invoice_from_delivery_note(source)
        if target.meta.has_field("is_return"):
            target.is_return = cint(getattr(source, "is_return", 0))
    sync_sales_invoice_links(target)

    target.set_ef_currency_from_settings()
    target.apply_ef_conversion_rate_rules()
    target.apply_vat()

    for d in target.items or []:
        d.ef_uom = d.ef_uom or d.uom
        d.ef_qty = d.ef_qty or d.qty


def _fallback_uom():
    for name in ("Nos", "Buc", "buc", "Unit"):
        if frappe.db.exists("UOM", name):
            return name
    uoms = frappe.get_all("UOM", pluck="name", limit=1)
    return uoms[0] if uoms else None


def _get_vat_rate_from_item_tax_template(template_name, cache):
    if not template_name:
        return 0

    if template_name in cache:
        return cache[template_name]

    rate = 0
    try:
        tpl = frappe.get_doc("Item Tax Template", template_name)
        if tpl.taxes and tpl.taxes[0].tax_rate is not None:
            rate = flt(tpl.taxes[0].tax_rate)
    except Exception:
        rate = 0

    cache[template_name] = rate
    return rate


def _party_bank_link_field(prefix: str) -> str:
    """Form Link field that holds the Bank Account for an e-Factura party block."""
    if prefix == "supplier":
        return "company_bank_account"
    return f"{prefix}_bank_account"


def _local_bank_details(ba_name: str) -> tuple[str, str, str]:
    """IBAN, bank title and branch code from a Bank Account, without calling SFS."""
    if not ba_name:
        return "", "", ""
    ba = frappe.get_doc("Bank Account", ba_name)
    account = (ba.iban or ba.bank_account_no or "").strip()
    branch_code = (ba.branch_code or "").strip()
    bank_name = ""
    if ba.bank:
        bank_name = (frappe.db.get_value("Bank", ba.bank, "bank_name") or ba.bank or "").strip()
    return account, bank_name, branch_code


def _ensure_supplier_bank_details(efactura):
    """Copy Company Bank Account onto hidden eF supplier bank fields when they are empty."""
    if efactura.get("ef_supplier_bank_account"):
        return
    ba_name = efactura.get("company_bank_account")
    if not ba_name:
        return
    account, bank_name, bank_code = _local_bank_details(ba_name)
    if not account:
        frappe.throw(
            _("e-Factura XML Error: Company Bank Account {0} has no IBAN").format(ba_name)
        )
    efactura.ef_supplier_bank_account = account
    if not efactura.get("ef_supplier_bank_name"):
        efactura.ef_supplier_bank_name = bank_name
    if not efactura.get("ef_supplier_bank_code"):
        efactura.ef_supplier_bank_code = bank_code
    if efactura.name and not efactura.is_new():
        efactura.db_set("ef_supplier_bank_account", account, update_modified=False)
        if efactura.ef_supplier_bank_name:
            efactura.db_set(
                "ef_supplier_bank_name", efactura.ef_supplier_bank_name, update_modified=False
            )
        if efactura.ef_supplier_bank_code:
            efactura.db_set(
                "ef_supplier_bank_code", efactura.ef_supplier_bank_code, update_modified=False
            )


def _generate_invoice_xml(
    efactura, language, save_to_file=False, file_path="output.xml", document=True, declaration=True
):
    # Create root element
    if document:
        root = ET.Element("Documents")
        doc = ET.SubElement(root, "Document")
        supplier_info = ET.SubElement(doc, "SupplierInfo")
        additional_info = ET.SubElement(doc, "AdditionalInformation")
        ET.SubElement(additional_info, "id").text = str(efactura.name)
    else:
        root = supplier_info = ET.Element("SupplierInfo")

    if efactura.ef_series and efactura.ef_number:
        ET.SubElement(supplier_info, "Seria").text = str(efactura.ef_series)
        ET.SubElement(supplier_info, "Number").text = str(efactura.ef_number)

    ET.SubElement(supplier_info, "IssuedDate").text = datetime.combine(
        efactura.issue_date, datetime.min.time()
    ).isoformat()
    ET.SubElement(supplier_info, "DeliveryDate").text = datetime.combine(
        efactura.delivery_date, datetime.min.time()
    ).isoformat()

    _ensure_supplier_bank_details(efactura)

    # Validate required fields. Bank name/code may be empty in SFS XML;
    # IBAN is filled from Company Bank Account when the hidden eF field is blank.
    required_fields = [
        "ef_supplier_idno",
        "ef_supplier_name",
        "ef_supplier_address",
        "ef_supplier_taxpayer_type",
        "ef_supplier_bank_account",
        "ef_customer_idno",
        "ef_customer_name",
        "ef_customer_address",
        "ef_customer_taxpayer_type",
    ]

    for fieldname in required_fields:
        if not efactura.get(fieldname):
            label = efactura.meta.get_label(fieldname)
            
            frappe.throw(
                _("e-Factura XML Error: {0} ({1}) must not be empty").format(label, fieldname)
            )

    # Supplier
    supplier = ET.SubElement(
        supplier_info,
        "Supplier",
        {
            "IDNO": efactura.ef_supplier_idno or "",
            "CodTVA": efactura.ef_supplier_vat_id or "",
            "TaxpayerType": taxpayer_type_to_sfs(efactura.ef_supplier_taxpayer_type),
            "Title": efactura.ef_supplier_name or "",
            "Address": efactura.ef_supplier_address or "",
    },)

    ET.SubElement(
        supplier,
        "BankAccount",
        {
            "Account": efactura.ef_supplier_bank_account or "",
            "BranchTitle": efactura.ef_supplier_bank_name or "",
            "BranchCode": efactura.ef_supplier_bank_code or "",
    },)

    # Buyer
    buyer = ET.SubElement(
        supplier_info,
        "Buyer",
        {
            "IDNO": efactura.ef_customer_idno or "",
            "CodTVA": efactura.ef_customer_vat_id or "",
            "TaxpayerType": taxpayer_type_to_sfs(efactura.ef_customer_taxpayer_type),
            "Title": efactura.ef_customer_name or "",
            "Address": efactura.ef_customer_address or "",
    },)

    ET.SubElement(
        buyer,
        "BankAccount",
        {
            "Account": efactura.ef_customer_bank_account or "",
            "BranchTitle": efactura.ef_customer_bank_name or "",
            "BranchCode": efactura.ef_customer_bank_code or "",
    },)

    if efactura.ef_transporter_idno:
        # Transporter
        transporter = ET.SubElement(
            supplier_info,
            "Transporter",
            {
                "IDNO": efactura.ef_transporter_idno or "",
                "CodTVA": efactura.ef_transporter_vat_id or "",
                "TaxpayerType": taxpayer_type_to_sfs(efactura.ef_transporter_taxpayer_type),
                "Title": efactura.ef_transporter_name or "",
                "Address": efactura.ef_transporter_address or "",
        },)

        ET.SubElement(
            transporter,
            "BankAccount",
            {
                "Account": efactura.ef_transporter_bank_account or "",
                "BranchTitle": efactura.ef_transporter_bank_name or "",
                "BranchCode": efactura.ef_transporter_bank_code or "",
        },)

    ET.SubElement(supplier_info, "Total").text = efactura.ef_total and str(round(flt(efactura.ef_total), 2)) or "0.00"
    ET.SubElement(supplier_info, "TotalTVA").text = efactura.ef_vat_total and str(round(flt(efactura.ef_vat_total), 2)) or "0.00"

    # Merchandises
    merchandises = ET.SubElement(supplier_info, "Merchandises")

    for item in efactura.items:

        uom = frappe.get_doc("UOM", item.ef_uom)
        qty = item.ef_qty or 0

        if not qty:
            label = item.meta.get_label("Sales eFactura Item")
            frappe.throw(_("e-Factura XML Error: Item {0} {1} must not be 0").format(item.idx, label))

        ET.SubElement(
            merchandises,
            "Row",
            {
                "Code": item.item_code,
                "Name": item.item_name,
                "UnitOfMeasure": _(uom.print_name or uom.name, language),
                "Quantity": str(qty),
                "UnitPriceWithoutTVA": str(round(flt(item.ef_net_rate or 0), 2)),
                "TotalPriceWithoutTVA": str(round(flt(item.ef_net_amount or 0), 2)),
                "TVA": str(int(item.ef_vat_rate or 0)),
                "TotalTVA": str(round(flt(item.ef_vat_amount or 0), 2)),
                "TotalPrice": str(round(flt(item.ef_amount or 0), 2)),
        },)

    ET.SubElement(supplier_info, "IsFarma").text = "false"
    ET.SubElement(supplier_info, "CreationMotiv").text = "4" if efactura.type == "Transfer" else "5"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    
    if save_to_file:
        tree.write(
            file_path,
            encoding="utf-8",
            xml_declaration=declaration,
            method="xml",
            short_empty_elements=False,
        )
        return None

    xml_content = ET.tostring(
        root, encoding="utf-8", xml_declaration=declaration, method="xml", short_empty_elements=False
    )
    return xml_content