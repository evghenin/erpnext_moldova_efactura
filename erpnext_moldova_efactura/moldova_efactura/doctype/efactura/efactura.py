# Copyright (c) 2025, Evgheni Nemerenco and contributors
# For license information, please see license.txt

import json, base64, re, frappe, hashlib, uuid
import xml.etree.ElementTree as ET
from erpnext_moldova_efactura.utils.fiscal_status import determine_fiscal_status

from datetime import datetime
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import cint, flt
from erpnext_moldova_efactura.api_client import EFacturaAPIClient
from lxml import etree
from erpnext_moldova_efactura.tasks.status_sync import _extract_single_invoice_from_search_response, _extract_status_map

class eFactura(Document):
    def onload(self):
        if self.docstatus == 0:
            self.update_items_available_qty()

    def validate(self):
        self.set_ef_currency_from_settings()
        self.apply_ef_conversion_rate_rules()
        self.update_items_available_qty()
        self.set_status()

    def on_submit(self):
        self.set_status()

    def on_cancel(self):
        if self.ef_status != -1 and self.ef_status != 5:
            frappe.throw(
                _("eFactura can be cancelled only in Pending Registration or Canceled by Supplier status.")
            )
        self.set_status()

    def on_update(self):
        # Auto-fill parties data after saving the document (draft included).
        # Use db_set(update_modified=False) to avoid recursive saves.
        self._autofill_parties_from_efactura_api_after_save()

    def set_status(self):
        """
        Map to sync 'status' field:

        status               | docstatus | ef_status | e-Factura            
        -------------------------------------------------------------------
        Draft                |     0     |    any    | 
        Canceled             |     2     |    any    | 
        Pending Registration |     1     |    -1     |
        Registered as Draft  |     1     |     0     | Draft
        Signed by Supplier   |     1     |     1     | Signed by Supplier
        Rejected by Customer |     1     |     2     | Rejected by Customer
        Accepted by Customer |     1     |     3     | Accepted by Customer
        Canceled by Supplier |     1     |     5     | Canceled by Supplier
        Sent to Customer     |     1     |   7,9     | Sent to Customer
        Signed by Customer   |     1     |     8     | Signed by Customer
        Transportation       |     1     |    10     | Transportation

        """

        if self.is_new():
            return

        ef_status_labels = {
            -1: "Pending Registration",
            0:  "Registered as Draft",
            1:  "Signed by Supplier",
            2:  "Rejected by Customer",
            3:  "Accepted by Customer",
            5:  "Canceled by Supplier",
            7:  "Sent to Customer",
            8:  "Signed by Customer",
            9:  "Sent to Customer",
            10: "Transportation",
        }

        if self.docstatus == 0:
            self.status = "Draft"
        elif self.docstatus == 2:
            self.status = "Cancelled"
        elif self.docstatus == 1:
            if self.ef_status is None:
                self.db_set("ef_status", -1, update_modified=False) 

            self.status = ef_status_labels.get(self.ef_status)

        self.db_set("status", self.status, update_modified=False)

        # --- Update linked Sales Invoice fiscal status ---
        if self.reference_doctype == "Sales Invoice" and self.reference_name:
            try:
                si = frappe.get_doc("Sales Invoice", self.reference_name)
                new_status = determine_fiscal_status(si)
                si.db_set("fiscal_status", new_status, update_modified=False)
            except frappe.ValidationError:
                # configuration error or blocked state – do not break eFactura flow
                pass

    def update_items_available_qty(self):
        if self.reference_doctype != "Sales Invoice" or not self.reference_name:
            return

        for item in self.items:
            if not item.item_code:
                continue
            total_si_stock_qty = (
                frappe.db.get_value(
                    "Sales Invoice Item",
                    {"parent": self.reference_name, "item_code": item.item_code},
                    "sum(stock_qty)",
                )
                or 0
            )
            efactura_names = frappe.get_all(
                "eFactura",
                filters={
                    "docstatus": 1,
                    "reference_name": self.reference_name,
                    "name": ["!=", self.name],
                },
                pluck="name",
            )
            used_stock_qty = (
                frappe.db.get_value(
                    "eFactura Item",
                    {"item_code": item.item_code, "parent": ["in", efactura_names]},
                    "sum(stock_qty)",
                )
                or 0
            )
            item.available_stock_qty = total_si_stock_qty - used_stock_qty

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

    def apply_vat(self):
        vat_included = cint(
            frappe.db.get_single_value("eFactura Settings", "vat_included_in_rate") or 0
        )
        ef_conv = flt(True)

        tpl_cache = {}
        self.ef_vat_total = 0
        self.ef_net_total = 0
        self.ef_total = 0
        self.net_total = 0
        self.vat_total = 0

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
            d.ef_vat_rate = vat_rate

            if not vat_rate:
                d.ef_net_rate = ef_rate
                d.ef_net_amount = ef_amount
                d.ef_vat_amount = 0
                d.ef_rate = ef_rate
                d.ef_amount = ef_amount
                continue

            if vat_included:
                # rate includes VAT -> ef_amount is gross
                divider = 1 + vat_rate / 100
                net_amount = amount / divider if divider else amount
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
            self.vat_total += d.vat_amount
            self.net_total += d.net_amount
            self.ef_total += d.ef_amount

    def _autofill_parties_from_efactura_api_after_save(self):
        # Prevent recursion
        if getattr(self.flags, "ef_autofill_running", False):
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

            client = EFacturaAPIClient.from_settings()

            self._autofill_party_block(
                client,
                "supplier",
                self.supplier_party_type,
                self.supplier_party,
                idno_fields[self.supplier_party_type],
            )
            self._autofill_party_block(
                client,
                "customer",
                self.customer_party_type,
                self.customer_party,
                idno_fields[self.customer_party_type],
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
            taxpayer_type = taxpayer.get("TaxpayerType") or ""
            is_user = "Yes" if taxpayer.get("IsEFacturaActor") else "No"

            self.db_set(f"ef_{prefix}_idno", idno, update_modified=False)
            self.db_set(f"ef_{prefix}_vat_id", vat_id, update_modified=False)
            self.db_set(f"ef_{prefix}_name", name, update_modified=False)
            self.db_set(f"ef_{prefix}_address", address, update_modified=False)
            self.db_set(f"ef_{prefix}_taxpayer_type", taxpayer_type, update_modified=False)
            self.db_set(f"ef_{prefix}_is_user", is_user, update_modified=False)

        # 2) GetBankAccountInfo (only if we already have bank account in doc)
        ba_field = f"{prefix}_bank_account"

        if ba_field in self.get_valid_columns():
            ba_name = getattr(self, ba_field, None) or ""

            if ba_name:
                ba = frappe.get_doc("Bank Account", ba_name)

                if ba.iban and ba.iban != getattr(self, f"ef_{prefix}_bank_account", None):
                    bank_resp = client.get_bank_account_info(
                        idno=party_idno, account_number=ba.iban
                    )
                    bank_accounts = (bank_resp.get("Results") or {}).get("BankAccount") or []

                    for bank in bank_accounts or []:
                        if bank.get("AccountNumber") == ba.iban:
                            bank_account = bank.get("AccountNumber") or ""
                            bank_name = bank.get("BranchTitle") or ""
                            bank_code = bank.get("BranchCode") or ""
                else:
                    bank_account = getattr(self, f"ef_{prefix}_bank_account", "")
                    bank_name = getattr(self, f"ef_{prefix}_bank_name", "")
                    bank_code = getattr(self, f"ef_{prefix}_bank_code", "")
            else:
                bank_account = ""
                bank_name = ""
                bank_code = ""

            self.db_set(f"ef_{prefix}_bank_account", bank_account, update_modified=False)
            self.db_set(f"ef_{prefix}_bank_name", bank_name, update_modified=False)
            self.db_set(f"ef_{prefix}_bank_code", bank_code, update_modified=False)

@frappe.whitelist()
def download_xml(efactura_name):
    efactura = frappe.get_doc("eFactura", efactura_name)
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
    client = EFacturaAPIClient.from_settings()
    efactura = frappe.get_doc("eFactura", efactura_name)

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
                efactura.db_set("ef_series", remote_series, update_modified=False)
                efactura.db_set("ef_number", remote_number, update_modified=False)
                efactura.db_set("ef_status", remote_status, update_modified=False)
                efactura.set_status()      

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

        if status is not None and status != efactura.ef_status:
            efactura.db_set("ef_status", status, update_modified=False)
            efactura.set_status()


@frappe.whitelist()
def download_pdf(efactura_name):
    efactura = frappe.get_doc("eFactura", efactura_name)

    client = EFacturaAPIClient.from_settings()
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
def get_for_sign(efactura_name):
    efactura = frappe.get_doc("eFactura", efactura_name)   
    ef_lang = frappe.db.get_single_value("eFactura Settings", "language")

    if not efactura.ef_series or not efactura.ef_number:
        client = EFacturaAPIClient.from_settings()
        resp = client.get_series_and_numbers(count=1)
        data = resp.get("Results", {}).get("SeriaAndNumber", [{}])[0]

        efactura.db_set("ef_series", data.get("Seria"))
        efactura.db_set("ef_number", data.get("Number"))

        if not efactura.ef_series or not efactura.ef_number:
            frappe.throw(_("e-Factura API Error: Unable to obtain Series and Number"))

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
    efactura = frappe.get_doc("eFactura", efactura_name)
    ef_lang = frappe.db.get_single_value("eFactura Settings", "language")

    client = EFacturaAPIClient.from_settings()

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
        efactura.db_set("ef_status", 0, update_modified=False)
        efactura.set_status()
        # series and number are assigned only after signing in eFactura system, 
        # so we need to clear them for unsigned invoices to avoid confusion
        efactura.db_set("ef_series", None, update_modified=False)
        efactura.db_set("ef_number", None, update_modified=False)
        return {
            "message": _("Successfully sent {0} unsigned invoice(s) to e-Factura system.").format(
                posted
        )}


@frappe.whitelist()
def update_dates(efactura_name, issue_date, delivery_date):
    """Update issue_date and delivery_date for submitted eFactura in Pending status."""
    if not efactura_name:
        frappe.throw(_("Missing eFactura document name."))

    ef = frappe.get_doc("eFactura", efactura_name)

    if ef.docstatus != 1:
        frappe.throw(_("Dates can be updated only for submitted documents."))

    if ef.ef_status != -1:
        frappe.throw(_("Dates can be updated only in Pending Registration status."))

    if not issue_date or not delivery_date:
        frappe.throw(_("Both Issue Date and Delivery Date are required."))

    # Normalize to YYYY-MM-DD
    issue_date = frappe.utils.getdate(issue_date)
    delivery_date = frappe.utils.getdate(delivery_date)

    ef.db_set("issue_date", issue_date, update_modified=False)
    ef.db_set("delivery_date", delivery_date, update_modified=False)

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

    ef = frappe.get_doc("eFactura", name)

    # Send signed XML via PostInvoices
    client = EFacturaAPIClient.from_settings()

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
    ef.db_set("ef_status", 1, update_modified=False)
    ef.set_status()

    return {
        "message": _("Successfully sent {0} signed invoice(s) to e-Factura system.").format(posted),
        "total": total,
        "posted": posted,
    }

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
                "doctype": "eFactura",
                # "field_map": {"is_return": "is_return"},
                "validation": {"docstatus": ["=", 1]},
            },
            "Delivery Note Item": {
                "doctype": "eFactura Item",
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
                    "sales_invoice": "sales_invoice",
        },},},
        target_doc,
        _set_missing_values,
    )
    doc.update_items_available_qty()

    return doc


@frappe.whitelist()
def make_efactura_from_sales_invoice(source_name, target_doc=None):

    doc = get_mapped_doc(
        "Sales Invoice",
        source_name,
        {
            "Sales Invoice": {
                "doctype": "eFactura",
            },
            "Sales Invoice Item": {
                "doctype": "eFactura Item",
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
        },},},
        target_doc,
        _set_missing_values,
    )
    doc.update_items_available_qty()

    return doc


def _set_missing_values(source, target):
    # Parent fields
    target.company = source.company
    target.reference_doctype = "Sales Invoice"
    target.reference_name = source.name
    target.currency = source.currency

    # Optional but usually correct
    target.customer_party_type = "Customer"
    target.customer_party = source.customer

    # If you want: set dates (optional)

    target.set_ef_currency_from_settings()
    target.apply_ef_conversion_rate_rules()
    target.apply_vat()

    for d in target.items or []:
        d.ef_uom = d.ef_uom or d.uom
        d.ef_qty = d.ef_qty or d.qty

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

    # Validate required fields
    required_fields = [
        "ef_supplier_idno",
        "ef_supplier_name",
        "ef_supplier_address",
        "ef_supplier_taxpayer_type",
        "ef_supplier_bank_account",
        "ef_supplier_bank_name",
        "ef_supplier_bank_code",
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
            "TaxpayerType": efactura.ef_supplier_taxpayer_type or "",
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
            "TaxpayerType": efactura.ef_customer_taxpayer_type or "",
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
                "TaxpayerType": efactura.ef_transporter_taxpayer_type or "",
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
            label = item.meta.get_label("eFactura Item")
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