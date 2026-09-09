"""Clear ERP document links when a fiscal document is cancelled."""

from __future__ import annotations

import frappe


def clear_item_fields(doc, fields: tuple[str, ...]) -> None:
	for row in doc.get("items") or []:
		updates = {}
		for field in fields:
			if getattr(row, field, None):
				setattr(row, field, None)
				updates[field] = ""
		if updates and row.name:
			frappe.db.set_value(row.doctype, row.name, updates, update_modified=False)


def clear_header_fields(doc, fields: tuple[str, ...]) -> None:
	updates = {}
	for field in fields:
		if not doc.meta.has_field(field):
			continue
		if getattr(doc, field, None):
			setattr(doc, field, None)
			updates[field] = ""
	if updates and doc.name:
		frappe.db.set_value(doc.doctype, doc.name, updates, update_modified=False)


def reverse_names(doctype: str, field: str, value: str) -> list[str]:
	if not value or not frappe.get_meta(doctype).has_field(field):
		return []
	return frappe.get_all(doctype, filters={field: value}, pluck="name")


def clear_reverse(doctype: str, field: str, names: list[str], expected: str | None = None) -> None:
	if not frappe.get_meta(doctype).has_field(field):
		return
	for name in names or []:
		if not name:
			continue
		current = frappe.db.get_value(doctype, name, field)
		if not current:
			continue
		if expected and current != expected:
			continue
		frappe.db.set_value(doctype, name, field, "", update_modified=False)


def merge_unique(*groups) -> list[str]:
	names: list[str] = []
	seen: set[str] = set()
	for group in groups:
		for name in group or []:
			if name and name not in seen:
				seen.add(name)
				names.append(name)
	return names
