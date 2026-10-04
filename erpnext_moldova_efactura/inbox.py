# Copyright (c) 2026, Evgheni Nemerenco and contributors
# For license information, please see license.txt

from __future__ import annotations

import re

import frappe
from frappe.utils import cint

INBOX_APP = "erpnext_moldova_inbox"


def inbox_installed() -> bool:
	return INBOX_APP in frappe.get_installed_apps()


def _wildcard(pattern: str, value: str) -> bool:
	pattern = (pattern or "").strip()
	if not pattern:
		return True
	rx = ".*".join(re.escape(part) for part in pattern.split("%"))
	return re.fullmatch(rx, value or "", flags=re.IGNORECASE) is not None


def rule_matches(rule, sender_name: str, sender_email: str, subject: str, file_name: str) -> bool:
	pairs = (
		(rule.sender_name, sender_name),
		(rule.sender_email, sender_email),
		(rule.subject, subject),
		(rule.file_name, file_name),
	)
	if not any((pattern or "").strip() for pattern, _value in pairs):
		return False
	if not cint(rule.import_via_pdf) and not cint(rule.import_via_ai):
		return False
	return all(_wildcard(pattern, value) for pattern, value in pairs)


def handle_inbound_email(inbound_email: str):
	"""Import factura attachments from a Supplier Inbox Email. Return None when nothing matches."""
	if not inbox_installed():
		return None
	if not cint(frappe.db.get_single_value("eFactura Settings", "inbox_import_enabled")):
		return None

	mail = frappe.get_doc("Supplier Inbox Email", inbound_email)
	rules = frappe.get_single("eFactura Settings").get("inbox_rules") or []
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Supplier Inbox Email", "attached_to_name": mail.name},
		fields=["file_name", "file_url"],
	)
	created = None
	failures = []
	for file_row in files:
		rule = next(
			(
				candidate
				for candidate in rules
				if rule_matches(
					candidate,
					mail.original_sender_name,
					mail.original_sender_email,
					mail.original_subject,
					file_row.file_name,
				)
			),
			None,
		)
		if not rule:
			continue
		try:
			name = _import_with_rule(file_row.file_url, file_row.file_name, rule)
		except Exception:
			failures.append(file_row.file_name or file_row.file_url)
			continue
		if name and not created:
			created = name
	if failures and not created:
		raise Exception(f"eFactura inbox import failed for {', '.join(failures)}")
	if not created:
		return None
	return {"doctype": "Purchase Factura", "name": created}


def _import_with_rule(file_url: str, file_name: str, rule):
	methods = []
	if cint(rule.import_via_pdf):
		methods.append(0)
	if cint(rule.import_via_ai):
		methods.append(1)
	last_error = None
	for use_ai in methods:
		try:
			return _import_for_company(file_url, use_ai)
		except Exception as exc:
			last_error = exc
			frappe.log_error(
				title="eFactura inbox import failed",
				message=f"{file_name or file_url}: {'AI' if use_ai else 'PDF'}\n{frappe.get_traceback()}",
			)
	if last_error:
		raise last_error
	raise Exception(f"No import method for {file_name or file_url}")


def _companies() -> list[str]:
	companies = [
		row.company
		for row in frappe.get_single("eFactura Settings").get("company_settings") or []
		if row.company
	]
	if companies:
		return list(dict.fromkeys(companies))
	return frappe.get_all("Company", pluck="name")


def _import_for_company(file_url: str, use_ai: int):
	from erpnext_moldova_efactura.moldova_efactura.doctype.purchase_factura.purchase_factura import (
		import_pdf,
	)

	last_error = None
	companies = _companies()
	if not companies:
		raise Exception("No company is available for the Supplier Inbox import")
	for company in companies:
		try:
			return import_pdf(file_url, company, use_ai=use_ai)
		except Exception as exc:
			last_error = exc
			if "does not match" not in str(exc):
				raise
	if last_error:
		raise last_error
	raise Exception("No company matched the factura")
