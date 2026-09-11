from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

import frappe
import requests
from lxml import etree
from zeep import Client, Settings
from zeep.exceptions import Fault, TransportError
from zeep.helpers import serialize_object
from zeep.plugins import HistoryPlugin
from zeep.transports import Transport
from zeep.wsse.username import UsernameToken


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


    def _dump_soap_envelope(self, envelope) -> str:
        if envelope is None:
            return ""
        try:
            return etree.tostring(envelope, pretty_print=True, encoding="unicode")
        except Exception as e:
            return f"<failed to dump xml: {e}>"

    def _redact_secrets(self, text: str) -> str:
        if not text:
            return text
        if self.password:
            text = text.replace(self.password, "***")
        return text

    def _json_snippet(self, value, limit: int = 50000) -> str:
        try:
            text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
        except Exception:
            text = repr(value)
        if len(text) > limit:
            return text[:limit] + "\n…[truncated]"
        return text

    def _log_failed_call(
        self,
        method_name: str,
        *,
        error: str,
        request: Optional[dict] = None,
        extra: Optional[dict] = None,
        response: Any = None,
    ) -> None:
        parts = [f"method={method_name}", f"error={error}"]
        payload = {}
        if request is not None:
            payload["request"] = request
        if extra:
            payload.update(extra)
        if payload:
            parts.append("payload=\n" + self._json_snippet(payload))
        if response is not None:
            parts.append("response=\n" + self._json_snippet(response))

        sent = getattr(self._history, "last_sent", None) or {}
        received = getattr(self._history, "last_received", None) or {}
        if sent.get("envelope") is not None:
            parts.append("SOAP REQUEST:\n" + self._dump_soap_envelope(sent["envelope"]))
        if received.get("envelope") is not None:
            parts.append("SOAP RESPONSE:\n" + self._dump_soap_envelope(received["envelope"]))

        try:
            frappe.log_error(
                title=f"SFS API {method_name} failed",
                message=self._redact_secrets("\n\n".join(parts)),
            )
        except Exception:
            pass


    @classmethod
    def from_settings(cls, company):
        from erpnext_moldova_efactura.utils.company_api import resolve_api_credentials

        creds = resolve_api_credentials(company)
        return cls(
            wsdl_url=creds["wsdl_url"],
            username=creds["username"],
            password=creds["password"],
            timeout=creds["timeout"],
            verify_tls=creds["verify_tls"],
            service_name=creds["service_name"],
            port_name=creds["port_name"],
        )

    def _new_request_id(self) -> str:
        return str(uuid.uuid4())

    def _call(self, method_name: str, request: Optional[dict] = None, **kwargs) -> Dict[str, Any]:
        from erpnext_moldova_efactura.utils.api_response import sfs_action_error

        method = getattr(self.service, method_name)
        extra = dict(kwargs)

        try:
            if request is not None:
                resp = method(request, **kwargs)
            else:
                resp = method(**kwargs)
            data = serialize_object(resp, dict)
        except Fault as e:
            error = f"SOAP Fault in {method_name}: {e.message or str(e)}"
            self._log_failed_call(method_name, error=error, request=request, extra=extra)
            raise EFacturaAPIError(error) from e
        except TransportError as e:
            error = f"Transport error in {method_name}: {str(e)}"
            self._log_failed_call(method_name, error=error, request=request, extra=extra)
            raise EFacturaAPIError(error) from e
        except Exception as e:
            error = f"Unexpected error in {method_name}: {str(e)}"
            self._log_failed_call(method_name, error=error, request=request, extra=extra)
            raise EFacturaAPIError(error) from e

        business_error = sfs_action_error(data)
        if business_error:
            self._log_failed_call(
                method_name,
                error=str(business_error),
                request=request,
                extra=extra,
                response=data,
            )
        return data

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
