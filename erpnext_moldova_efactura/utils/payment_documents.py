"""Outgoing e-Factura attached documents taken from Payment Entries."""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime

import frappe
from frappe.utils import cint, flt, getdate

_MEV_URL = re.compile(r"https?://[^\s\"'<>]*mev\.sfs\.md[^\s\"'<>]*", re.IGNORECASE)
_FILE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def document_names_by_mode() -> dict[str, str]:
	if not cint(frappe.db.get_single_value("eFactura Settings", "attach_payment_documents")):
		return {}
	rows = frappe.get_all(
		"eFactura Payment Document",
		filters={"parent": "eFactura Settings", "parenttype": "eFactura Settings"},
		fields=["mode_of_payment", "document_name"],
	)
	names = {}
	for row in rows:
		mode = (row.mode_of_payment or "").strip()
		title = (row.document_name or "").strip()
		if mode and title:
			names[mode] = title
	return names


def payments_for_sales_invoice(sales_invoice: str) -> list[dict]:
	if not sales_invoice:
		return []
	return frappe.db.sql(
		"""
		SELECT pe.name, pe.mode_of_payment, pe.reference_no, pe.reference_date,
			pe.remarks, pe.posting_date, per.allocated_amount
		FROM `tabPayment Entry` pe
		INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
		WHERE per.reference_doctype = 'Sales Invoice'
			AND per.reference_name = %(sales_invoice)s
			AND pe.docstatus = 1
		ORDER BY pe.posting_date, pe.name
		""",
		{"sales_invoice": sales_invoice},
		as_dict=True,
	)


def mev_urls(*texts: str | None) -> list[str]:
	found = []
	seen = set()
	for text in texts:
		if not text:
			continue
		for match in _MEV_URL.findall(text):
			url = match.rstrip(".,);")
			if url not in seen:
				seen.add(url)
				found.append(url)
	return found


def comment_text(payment_entry: str) -> str:
	rows = frappe.get_all(
		"Comment",
		filters={
			"reference_doctype": "Payment Entry",
			"reference_name": payment_entry,
			"comment_type": "Comment",
		},
		fields=["content"],
	)
	return "\n".join((row.content or "") for row in rows)


def document_number(reference_no, allocated_amount, urls: list[str]) -> str:
	parts = []
	number = (reference_no or "").strip()
	if number:
		parts.append(number)
	if flt(allocated_amount):
		parts.append(f"{flt(allocated_amount):.2f}")
	parts.extend(urls)
	return " ".join(parts)


def attached_document_rows(sales_invoice: str) -> list[dict]:
	names = document_names_by_mode()
	if not names or not sales_invoice:
		return []
	rows = []
	for payment in payments_for_sales_invoice(sales_invoice):
		title = names.get((payment.mode_of_payment or "").strip())
		if not title:
			continue
		urls = mev_urls(payment.remarks, comment_text(payment.name))
		row = {"type": title, "number": document_number(payment.reference_no, payment.allocated_amount, urls)}
		if payment.reference_date:
			row["date"] = datetime.combine(getdate(payment.reference_date), datetime.min.time()).isoformat()
		row["payment_entry"] = payment.name
		rows.append(row)
	return rows


def append_attached_documents(supplier_info, rows: list[dict]) -> None:
	if not rows:
		return
	import xml.etree.ElementTree as ET

	parent = ET.SubElement(supplier_info, "AttachedDocuments")
	for row in rows:
		attrs = {"Type": row["type"]}
		if row.get("number"):
			attrs["Number"] = row["number"]
		if row.get("date"):
			attrs["Date"] = row["date"]
		ET.SubElement(parent, "Document", attrs)


def first_payment_attachment(payment_names: list[str]) -> dict | None:
	if not payment_names:
		return None
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Payment Entry", "attached_to_name": ["in", payment_names]},
		fields=["name", "file_name", "attached_to_name"],
		order_by="creation asc",
	)
	by_payment: dict[str, list] = {}
	for file_row in files:
		ext = os.path.splitext(file_row.file_name or "")[1].lower()
		if ext not in _FILE_EXTENSIONS:
			continue
		by_payment.setdefault(file_row.attached_to_name, []).append(file_row)
	for payment_name in payment_names:
		matches = by_payment.get(payment_name) or []
		if not matches:
			continue
		file_doc = frappe.get_doc("File", matches[0].name)
		content = file_doc.get_content()
		if not content:
			continue
		if isinstance(content, str):
			content = content.encode()
		return {
			"FileName": file_doc.file_name,
			"FileContent": base64.b64encode(content).decode(),
		}
	return None
