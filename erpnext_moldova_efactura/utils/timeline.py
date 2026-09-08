"""Timeline for eFactura actions.

SFS updates use db_set(update_modified=False), so Frappe does not create
Version rows. Status diffs are written as Version. User actions are Info
logs (not Comment).

Activity text is stored as an English msgid + args JSON payload so the desk
timeline can translate it in the viewer's language (like Version field diffs).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.core.doctype.version.version import Version
from frappe.model.document import Document
from frappe.utils import cstr

VERSION_IGNORE_FIELDS = ("last_status_check",)
EF_LOG_PREFIX = "efj:"
EF_LOG_LEGACY_PREFIX = "ef:"


def log_event(doc, msgid: str, *args, translate_args: list[int] | tuple[int, ...] | None = None) -> None:
	"""Append an Info timeline log with a deferred-translation pattern.

	Stores English ``msgid`` and ``args`` (not a pre-translated sentence).
	Desk JS renders ``You {translated}`` / ``{user} {translated}``.
	"""
	if not doc or not getattr(doc, "name", None) or doc.is_new():
		return
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_import:
		return
	try:
		msgid = cstr(msgid or "").strip()
		if not msgid:
			return
		# Strip accidental legacy prefixes from callers.
		if msgid.startswith(EF_LOG_PREFIX):
			msgid = msgid[len(EF_LOG_PREFIX) :]
		elif msgid.startswith(EF_LOG_LEGACY_PREFIX):
			msgid = msgid[len(EF_LOG_LEGACY_PREFIX) :]

		payload = {
			"msgid": msgid,
			"args": [cstr(a) for a in args],
		}
		if translate_args:
			payload["translate_args"] = [int(i) for i in translate_args]
		doc.add_comment("Info", EF_LOG_PREFIX + frappe.as_json(payload, indent=None, separators=(",", ":")))
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


def _extractable_log_messages():
	"""Msgid strings for babel / bench translation extraction. Do not call at runtime."""
	return (
		_("assigned series and number {0}{1} from e-Factura"),
		_("assigned series and number {0}{1} for signing this document"),
		_("canceled this document in e-Factura: {0}"),
		_("sent unsigned invoice to e-Factura (draft)"),
		_("updated Issue Date / Delivery Date: {0} / {1} → {2} / {3}"),
		_("sent signed invoice to e-Factura"),
		_("unmarked this document as return"),
		_("marked this document as return"),
		_("linked Purchase Receipt Return {0}"),
		_("unlinked Purchase Receipt Return"),
		_("fetched invoice details from e-Factura"),
		_("accepted this document in e-Factura"),
		_("rejected this document in e-Factura: {0}"),
		_("signed this document in e-Factura (buyer)"),
		_("linked Purchase Invoice {0}"),
		_("linked {0} {1}"),
		_("unlinked Purchase Invoice"),
		_("unlinked {0}"),
		_("unlinked Purchase Order"),
	)
