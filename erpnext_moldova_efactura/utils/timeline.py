"""Timeline for eFactura actions.

SFS updates use db_set(update_modified=False), so Frappe does not create
Version rows. Status diffs are written as Version (not Comment). User
actions (accept, sign, fetch) stay as comments.
"""

from __future__ import annotations

import frappe
from frappe.core.doctype.version.version import Version
from frappe.model.document import Document
from frappe.utils import cstr

VERSION_IGNORE_FIELDS = ("last_status_check",)


def log_event(doc, message: str) -> None:
	if not doc or not getattr(doc, "name", None) or doc.is_new():
		return
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_import:
		return
	try:
		doc.add_comment("Comment", message)
	except Exception:
		frappe.log_error(title="eFactura timeline log failed", message=frappe.get_traceback())


def log_status_change(doc, old_status, new_status) -> None:
	old_label = cstr(old_status or "").strip()
	new_label = cstr(new_status or "").strip()
	if not new_label or old_label == new_label:
		return
	if getattr(doc, "flags", None) and doc.flags.get("in_validate"):
		return
	_insert_version(doc, [["status", old_label, new_label]])


def _insert_version(doc, changed: list[list]) -> None:
	if not doc or not getattr(doc, "name", None) or doc.is_new():
		return
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_import:
		return
	changed = [row for row in changed if (row[1] or "") != (row[2] or "")]
	if not changed:
		return
	try:
		version = frappe.new_doc("Version")
		data = {"changed": changed}
		Version.set_impersonator(data)
		version.ref_doctype = doc.doctype
		version.docname = doc.name
		version.data = frappe.as_json(data, indent=None, separators=(",", ":"))
		version.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="eFactura version log failed", message=frappe.get_traceback())


def save_doc_version(doc) -> None:
	"""Frappe Version without noisy system-field diffs (Last Status Check)."""
	before = doc.get_doc_before_save()
	saved = {}
	if before:
		for field in VERSION_IGNORE_FIELDS:
			if doc.meta.has_field(field):
				saved[field] = doc.get(field)
				doc.set(field, before.get(field))
	try:
		Document.save_version(doc)
	finally:
		for field, val in saved.items():
			doc.set(field, val)
