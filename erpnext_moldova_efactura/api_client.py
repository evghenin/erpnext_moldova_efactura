from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

import frappe
from frappe import _
import requests
from requests.auth import HTTPBasicAuth
from zeep import Client, Settings
from zeep.exceptions import Fault, TransportError
from zeep.helpers import serialize_object
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken
from zeep.plugins import HistoryPlugin
from lxml import etree


class EFacturaAPIError(Exception):
    pass


class EFacturaAPIClient:
    """
    e-Factura SOAP client
    Authentication via HTTP Basic Auth (requests.Session.auth)
    """

    def __init__(self, wsdl_url, username, password, timeout=20, verify_tls=True, service_name=None, port_name=None):
        self.wsdl_url = wsdl_url.rstrip("?wsdl") + "?wsdl"
        self.username = username
        self.password = password

        session = requests.Session()
        # session.auth = HTTPBasicAuth(username, password)
        session.verify = verify_tls
        session.headers.update({"User-Agent": "erpnext-moldova-efactura/1.0"})

        transport = Transport(session=session, timeout=timeout)
        wsse = UsernameToken(username, password, use_digest=False)
        settings = Settings(strict=False, xml_huge_tree=True)

        history = HistoryPlugin()

        client = Client(
            wsdl=wsdl_url,
            transport=transport,
            settings=settings,
            wsse=wsse,
            plugins=[history],
        )

        self._history = history

        # client = Client(wsdl=wsdl_url, transport=transport, settings=settings, wsse=wsse)

        # --- Pick service/port ---
        services = client.wsdl.services
        if not services:
            raise RuntimeError("No SOAP services found in WSDL.")

        if not service_name:
            service_name = next(iter(services.keys()))
        service = services.get(service_name)
        if not service:
            raise RuntimeError(f"Service '{service_name}' not found in WSDL. Available: {list(services.keys())}")

        if not port_name:
            port_name = next(iter(service.ports.keys()))
        if port_name not in service.ports:
            raise RuntimeError(f"Port '{port_name}' not found in service '{service_name}'. Available: {list(service.ports.keys())}")

        self._client = client
        self.service = client.bind(service_name, port_name)

        # Fallback (should not be needed, but safe)
        if self.service is None:
            self.service = client.service


    def _dump_soap_envelope(self, label: str, envelope):
        if envelope is None:
            return
        try:
            xml_str = etree.tostring(
                envelope,
                pretty_print=True,
                encoding="unicode",
            )
            frappe.log_error(f"{label}", xml_str)
        except Exception as e:
            frappe.log_error(f"{label}: failed to dump xml", str(e))


    @classmethod
    def from_settings(cls):
        s = frappe.get_single("eFactura Settings")

        wsdl_url = getattr(s, "api_wsdl_url", None) or getattr(s, "api_url", None)
        if not wsdl_url:
            frappe.throw(_("eFactura Settings: api_wsdl_url is not set."))

        username = getattr(s, "api_username", None)
        password = getattr(s, "api_password", None)
        if not username or not password:
            frappe.throw(_("eFactura Settings: API username/password are not set."))

        timeout = int(getattr(s, "api_timeout_seconds", 20) or 20)
        verify_tls = bool(getattr(s, "api_verify_tls", 1))

        service_name = getattr(s, "api_service_name", None)  # optional
        port_name = getattr(s, "api_port_name", None)        # optional

        return cls(
            wsdl_url=wsdl_url,
            username=username,
            password=password,
            timeout=timeout,
            verify_tls=verify_tls,
            service_name=service_name,
            port_name=port_name,
        )

    def _new_request_id(self) -> str:
        return str(uuid.uuid4())

    def _call(self, method_name: str, request: Optional[dict] = None, **kwargs) -> Dict[str, Any]:
        # try:
        method = getattr(self.service, method_name)
        # except AttributeError as e:
            # raise EFacturaAPIError(f"Unknown SOAP method: {method_name}") from e

        try:
            if request is not None:
                resp = method(request, **kwargs)
            else:
                resp = method(**kwargs)

            return serialize_object(resp, dict)

        except Fault as e:
            raise EFacturaAPIError(
                f"SOAP Fault in {method_name}: {e.message or str(e)}"
            ) from e
        except TransportError as e:
            raise EFacturaAPIError(
                f"Transport error in {method_name}: {str(e)}"
            ) from e
        except Exception as e:
            raise EFacturaAPIError(
                f"Unexpected error in {method_name}: {str(e)}"
            ) from e
        # finally:
            # dump last SOAP request/response (even if fault)
            # sent = getattr(self._history, "last_sent", None)
            # received = getattr(self._history, "last_received", None)

            # if sent and "envelope" in sent:
            #     self._dump_soap_envelope("eFactura SOAP REQUEST", sent["envelope"])
            # if received and "envelope" in received:
            #     self._dump_soap_envelope("eFactura SOAP RESPONSE", received["envelope"])

    # -------------------------
    # API methods
    # -------------------------

    def test(self, message: str) -> Dict[str, Any]:
        return self._call("Test", request=None, message=message)

    def get_taxpayers_info(self, fiscal_codes: list[str], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "FiscalCodes": {"string": fiscal_codes},
        }
        return self._call("GetTaxpayersInfo", request=req)

    def get_bank_account_info(
        self,
        idno: Optional[str] = None,
        account_number: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "IDNO": idno,
            "AccountNumber": account_number,
        }
        return self._call("GetBankAccountInfo", request=req)

    def get_series_and_numbers(
        self,
        count: int,
        start_number: Optional[int] = None,
        invoice_type: Optional[int] = None,
        series: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "Count": count,
            "StartNumber": start_number,
            "Seria": series,
            "InvoiceType": invoice_type,
        }
        return self._call("GetSeriaAndNumbers", request=req)

    def get_invoices_qrcodes(self, seria_and_numbers: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "SeriaAndNumbers": {"InvoiceIndentificator": seria_and_numbers},
        }
        return self._call("GetInvoicesQRcodes", request=req)

    def get_invoices_content_for_print(
        self,
        seria_and_numbers: list[dict],
        actor_role: Optional[int] = 0,
        orientation: Optional[int] = 0,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "SeriaAndNumbers": {"InvoiceIndentificator": seria_and_numbers},
            "ActorRole": actor_role,
            "Orientation": orientation,
        }
        return self._call("GetInvoicesContentForPrint", request=req)

    def get_invoices_by_seria_number(self, seria_and_numbers: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "SeriaAndNumbers": {"InvoiceIndentificator": seria_and_numbers},
        }
        return self._call("GetInvoicesBySeriaNumber", request=req)

    def check_invoices_status(self, seria_and_numbers: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "SeriaAndNumbers": {"InvoiceIndentificator": seria_and_numbers},
        }
        return self._call("CheckInvoicesStatus", request=req)

    def get_invoices_for_signing(self, actor_role: int, order: int, request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
            "Order": order,
        }
        return self._call("GetInvoicesForSigning", request=req)

    def get_accepted_invoices(self, actor_role: int, request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
        }
        return self._call("GetAcceptedInvoices", request=req)

    def get_rejected_invoices(self, actor_role: int, request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
        }
        return self._call("GetRejectedInvoices", request=req)

    def post_accepted_invoices(self, seria_and_numbers: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "SeriaAndNumbers": {"InvoiceIndentificator": seria_and_numbers},
        }
        return self._call("PostAcceptedInvoices", request=req)

    def post_rejected_invoices(self, invoices_comments: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "InvoicesComments": {"InvoiceComment": invoices_comments},
        }
        return self._call("PostRejectedInvoices", request=req)

    def post_canceled_invoices(self, invoices_comments: list[dict], request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "InvoicesComments": {"InvoiceComment": invoices_comments},
        }
        return self._call("PostCanceledInvoices", request=req)

    def post_invoices(
        self,
        actor_role: int,
        invoices_xml: str,
        invoices_xml_status: int,
        attachment: Optional[dict] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
            "InvoicesXml": invoices_xml,
            "InvoicesXmlStatus": invoices_xml_status,
            "Attachment": attachment,
        }
        return self._call("PostInvoices", request=req)

    def post_invoices_with_attachment(
        self,
        actor_role: int,
        invoices_xml: str,
        invoices_xml_status: int,
        attachment: Optional[dict],
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
            "InvoicesXml": invoices_xml,
            "InvoicesXmlStatus": invoices_xml_status,
            "Attachment": attachment,
        }
        return self._call("PostInvoicesWithAttachment", request=req)

    def search_invoices(self, actor_role: int, parameters: dict, request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "ActorRole": actor_role,
            "Parameters": parameters,
        }
        return self._call("SearchInvoices", request=req)

    def get_logs(self, date_from, date_to, request_id: Optional[str] = None) -> Dict[str, Any]:
        req = {
            "RequestId": request_id or self._new_request_id(),
            "From": date_from,
            "To": date_to,
        }
        return self._call("GetLogs", request=req)
