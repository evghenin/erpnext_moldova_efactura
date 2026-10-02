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
from zeep.utils import get_media_type
from zeep.wsse.username import UsernameToken


class EFacturaAPIError(Exception):
    pass


class _StatusTransport(Transport):
    """Zeep transport that keeps the last HTTP status for error logging."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_status_code = 0

    def post(self, address, message, headers):
        response = super().post(address, message, headers)
        status = int(getattr(response, "status_code", 0) or 0)
        self.last_status_code = status
        # Zeep otherwise embeds the raw body in TransportError ("Content: ...").
        # SFS HTTP 500 pages are HTML with a large SVG logo.
        if status == 500:
            media_type = get_media_type(response.headers.get("Content-Type", "text/xml"))
            if media_type not in ("text/xml", "application/xml", "application/soap+xml"):
                raise TransportError("HTTP 500", status_code=500)
        return response


def _http_status_code(exc: BaseException | None = None, transport=None) -> int:
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(transport, "last_status_code", None),
    ):
        try:
            status = int(candidate or 0)
        except (TypeError, ValueError):
            continue
        if status:
            return status
    return 0


def _parse_invoice_post_result(xml_text: str) -> Dict[str, Any]:
    root = etree.fromstring((xml_text or "").encode("utf-8"))
    ns = {"a": "http://schemas.datacontract.org/2004/07/AX.EFactura.Model.ApiModel"}

    def text(tag: str) -> str:
        element = root.find(f".//a:{tag}", ns)
        if element is None or element.text is None:
            return ""
        return element.text

    def number(tag: str) -> int:
        try:
            return int(text(tag) or 0)
        except ValueError:
            return 0

    return {
        "RequestId": text("RequestId"),
        "Status": number("Status"),
        "ErrorMessage": text("ErrorMessage") or None,
        "TotalInvoices": number("TotalInvoices"),
        "TotalInvoicesPosted": number("TotalInvoicesPosted"),
    }


def _looks_like_html_error_page(text: str) -> bool:
    sample = (text or "")[:4000].lower()
    return "<svg" in sample or "<html" in sample or "<!doctype html" in sample


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

        transport = _StatusTransport(session=session, timeout=timeout)
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

        self._transport = transport
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
        omit_response_body: bool = False,
    ) -> None:
        if _looks_like_html_error_page(error) or "\nContent:" in error:
            error = error.split("\nContent:", 1)[0]
            omit_response_body = True
            if _looks_like_html_error_page(error):
                error = "HTTP 500"
        parts = [f"method={method_name}", f"error={error}"]
        payload = {}
        if request is not None:
            payload["request"] = request
        if extra:
            payload.update(extra)
        if payload:
            parts.append("payload=\n" + self._json_snippet(payload))
        if response is not None and not omit_response_body:
            parts.append("response=\n" + self._json_snippet(response))

        sent = getattr(self._history, "last_sent", None) or {}
        received = getattr(self._history, "last_received", None) or {}
        if sent.get("envelope") is not None:
            parts.append("SOAP REQUEST:\n" + self._dump_soap_envelope(sent["envelope"]))
        if received.get("envelope") is not None and not omit_response_body:
            dumped = self._dump_soap_envelope(received["envelope"])
            if not _looks_like_html_error_page(dumped):
                parts.append("SOAP RESPONSE:\n" + dumped)

        title = f"SFS API {method_name} failed"
        ident = self._request_ident_label(request)
        if ident:
            title = f"{title} {ident}"
        try:
            frappe.log_error(
                title=title,
                message=self._redact_secrets("\n\n".join(parts)),
            )
        except Exception:
            pass

    def _omit_http_500_body(self, exc: BaseException | None = None) -> bool:
        return _http_status_code(exc, getattr(self, "_transport", None)) == 500

    @staticmethod
    def _request_ident_label(request: Optional[dict]) -> str:
        if not request:
            return ""
        items = (request.get("SeriaAndNumbers") or {}).get("InvoiceIndentificator") or []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            return ""
        if len(items) == 1:
            seria = str(items[0].get("Seria") or "").strip()
            number = str(items[0].get("Number") or "").strip()
            return f"{seria}{number}".strip()
        return f"({len(items)} invoices)"


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
            self._log_failed_call(
                method_name,
                error=error,
                request=request,
                extra=extra,
                omit_response_body=self._omit_http_500_body(e),
            )
            raise EFacturaAPIError(error) from e
        except TransportError as e:
            status = _http_status_code(e, getattr(self, "_transport", None))
            if status == 500:
                error = f"Transport error in {method_name}: HTTP 500"
            else:
                error = f"Transport error in {method_name}: {str(e)}"
            self._log_failed_call(
                method_name,
                error=error,
                request=request,
                extra=extra,
                omit_response_body=status == 500,
            )
            raise EFacturaAPIError(error) from None
        except Exception as e:
            error = f"Unexpected error in {method_name}: {str(e)}"
            self._log_failed_call(
                method_name,
                error=error,
                request=request,
                extra=extra,
                omit_response_body=self._omit_http_500_body(e),
            )
            raise EFacturaAPIError(error) from e

        business_error = sfs_action_error(data)
        if business_error:
            self._log_failed_call(
                method_name,
                error=str(business_error),
                request=request,
                extra=extra,
                response=data,
                omit_response_body=self._omit_http_500_body(),
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
        data = self._call("PostInvoices", request=req)
        self._log_if_invoices_not_posted("PostInvoices", req, data)
        return data

    def _log_if_invoices_not_posted(self, method_name: str, request: dict, data: Any) -> None:
        from erpnext_moldova_efactura.utils.api_response import sfs_action_error

        if not isinstance(data, dict) or sfs_action_error(data):
            return
        try:
            total = int(data.get("TotalInvoices") or 0)
            posted = int(data.get("TotalInvoicesPosted") or 0)
        except (TypeError, ValueError):
            total, posted = 0, 0
        if total == posted and posted:
            return
        self._log_failed_call(
            method_name,
            error=f"Invoices posted: {posted} / {total}",
            request=request,
            response=data,
        )

    def post_invoices_with_attachment(
        self,
        actor_role: int,
        invoices_xml: str,
        invoices_xml_status: int,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Post without a SOAP Attachment element. The PDF is already inside InvoicesXml."""
        request_id = request_id or self._new_request_id()
        req = {
            "RequestId": request_id,
            "ActorRole": actor_role,
            "InvoicesXml": invoices_xml,
            "InvoicesXmlStatus": invoices_xml_status,
        }
        endpoint = self.wsdl_url.split("?")[0]
        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:tem="http://tempuri.org/"'
            ' xmlns:ax="http://schemas.datacontract.org/2004/07/AX.EFactura.Model.ApiModel">'
            '<soapenv:Header xmlns:wsa="http://www.w3.org/2005/08/addressing">'
            "<wsa:Action>http://tempuri.org/IService/PostInvoicesWithAttachment</wsa:Action>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:To>{endpoint}</wsa:To>"
            "<wsse:Security"
            ' xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            "<wsse:UsernameToken>"
            f"<wsse:Username>{self.username}</wsse:Username>"
            "<wsse:Password"
            ' Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">'
            f"{self.password}</wsse:Password>"
            "</wsse:UsernameToken></wsse:Security></soapenv:Header>"
            "<soapenv:Body><tem:PostInvoicesWithAttachment><tem:request>"
            f"<ax:RequestId>{request_id}</ax:RequestId>"
            f"<ax:ActorRole>{int(actor_role)}</ax:ActorRole>"
            f"<ax:InvoicesXml><![CDATA[{invoices_xml}]]></ax:InvoicesXml>"
            f"<ax:InvoicesXmlStatus>{int(invoices_xml_status)}</ax:InvoicesXmlStatus>"
            "</tem:request></tem:PostInvoicesWithAttachment></soapenv:Body></soapenv:Envelope>"
        )
        try:
            response = self._transport.session.post(
                endpoint,
                data=envelope.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": "http://tempuri.org/IService/PostInvoicesWithAttachment",
                },
                timeout=self._transport.operation_timeout,
            )
        except Exception as e:
            error = f"Transport error in PostInvoicesWithAttachment: {e}"
            self._log_failed_call("PostInvoicesWithAttachment", error=error, request=req)
            raise EFacturaAPIError(error) from e
        self._transport.last_status_code = int(response.status_code or 0)
        if response.status_code >= 400:
            error = f"Transport error in PostInvoicesWithAttachment: HTTP {response.status_code}"
            self._log_failed_call(
                "PostInvoicesWithAttachment",
                error=error,
                request=req,
                omit_response_body=response.status_code == 500,
            )
            raise EFacturaAPIError(error)
        data = _parse_invoice_post_result(response.text)
        self._log_if_invoices_not_posted("PostInvoicesWithAttachment", req, data)
        return data

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
