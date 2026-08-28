"""Shared e-Factura download responses."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_moldova_efactura.api_client import EFacturaAPIClient


def download_sfs_pdf(doc, actor_role: int) -> None:
	"""Fetch a printable invoice from SFS and prepare the Frappe download response."""
	if not doc.ef_series or not doc.ef_number:
		frappe.throw(_("eFactura Series/Number is required to download PDF"))

	client = EFacturaAPIClient.from_settings(company=doc.company)
	response = client.get_invoices_content_for_print(
		seria_and_numbers={"Seria": doc.ef_series, "Number": doc.ef_number},
		actor_role=actor_role,
	)
	pdf_content = (response or {}).get("Result", {}).get("Content") or b""
	if not isinstance(pdf_content, bytes) or not pdf_content.startswith(b"%PDF"):
		frappe.throw(_("e-Factura returned non-PDF content in Result.Content"))

	frappe.local.response.filename = f"{doc.ef_series}{doc.ef_number}.pdf"
	frappe.local.response.filecontent = pdf_content
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/pdf"
