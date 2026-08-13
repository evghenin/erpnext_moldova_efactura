"""Resolve Item Code for eFactura Buyer lines."""

from __future__ import annotations

import frappe


def _norm_name(value: str | None) -> str:
	return (value or "").strip().lower()


def _names_match(a: str | None, b: str | None) -> bool:
	na, nb = _norm_name(a), _norm_name(b)
	return bool(na) and na == nb


def resolve_item_code(
	supplier: str | None,
	supplier_item_code: str | None,
	supplier_item_name: str | None = None,
) -> str | None:
	"""
	Auto-resolve Item for a buyer line.

	Order:
	1) By supplier_item_code — only trusted if supplier_item_name also matches
	   (code alone is unreliable / may be random)
	2) By supplier_item_name (stable product title):
	   - past eFactura Buyer of this supplier
	   - Supplier Item Map
	   - direct Item.item_name match
	"""
	code = (supplier_item_code or "").strip()
	name = (supplier_item_name or "").strip()

	if code:
		direct = _direct_item_match(code)
		if direct and _code_hit_trusted(direct, name, item_name_from_item=True):
			return direct

		if supplier:
			past_item, past_name = _from_past_invoices(supplier, supplier_item_code=code)
			if past_item and _code_hit_trusted(past_item, name, expected_supplier_name=past_name):
				return past_item

			mapped = frappe.db.get_value(
				"eFactura Supplier Item Map",
				{"supplier": supplier, "supplier_item_code": code},
				["item_code", "supplier_item_name"],
				as_dict=True,
			)
			if mapped and mapped.item_code and _code_hit_trusted(
				mapped.item_code, name, expected_supplier_name=mapped.supplier_item_name
			):
				return mapped.item_code

	if name:
		if supplier:
			past_item, _past_name = _from_past_invoices(supplier, supplier_item_name=name)
			if past_item:
				return past_item

			mapped = frappe.db.get_value(
				"eFactura Supplier Item Map",
				{"supplier": supplier, "supplier_item_name": name},
				"item_code",
				order_by="modified desc",
			)
			if mapped:
				return mapped

		by_item_name = frappe.db.get_value(
			"Item", {"item_name": name, "disabled": 0}, "name"
		)
		if by_item_name:
			return by_item_name

	return None


def _code_hit_trusted(
	item_code: str,
	current_supplier_name: str | None,
	*,
	expected_supplier_name: str | None = None,
	item_name_from_item: bool = False,
) -> bool:
	"""Code match is trusted only when product name agrees (if a name is present)."""
	if not current_supplier_name:
		# Nothing to verify — keep code hit
		return True

	if item_name_from_item:
		item_name = frappe.db.get_value("Item", item_code, "item_name")
		return _names_match(current_supplier_name, item_name)

	if expected_supplier_name is not None:
		return _names_match(current_supplier_name, expected_supplier_name)

	return True


def _direct_item_match(code: str) -> str | None:
	if frappe.db.exists("Item", code):
		disabled = frappe.db.get_value("Item", code, "disabled")
		if not disabled:
			return code

	matched = frappe.db.get_value("Item", {"item_code": code, "disabled": 0}, "name")
	return matched or None


def _from_past_invoices(
	supplier: str,
	supplier_item_code: str | None = None,
	supplier_item_name: str | None = None,
) -> tuple[str | None, str | None]:
	"""Return (item_code, supplier_item_name) from the latest matching buyer line."""
	conditions = [
		"p.supplier = %s",
		"IFNULL(i.item_code, '') != ''",
		"p.docstatus < 2",
	]
	values: list = [supplier]

	if supplier_item_code:
		conditions.append("i.supplier_item_code = %s")
		values.append(supplier_item_code)
	elif supplier_item_name:
		conditions.append("i.supplier_item_name = %s")
		values.append(supplier_item_name)
	else:
		return None, None

	rows = frappe.db.sql(
		f"""
		SELECT i.item_code, i.supplier_item_name
		FROM `tabeFactura Buyer Item` i
		INNER JOIN `tabeFactura Buyer` p ON p.name = i.parent
		WHERE {" AND ".join(conditions)}
		ORDER BY p.modified DESC
		LIMIT 1
		""",
		tuple(values),
	)
	if not rows:
		return None, None
	return rows[0][0], rows[0][1]


def upsert_item_map(
	supplier: str,
	supplier_item_code: str | None,
	supplier_item_name: str,
	item_code: str,
	uom: str | None = None,
):
	"""Persist mapping. Prefer stable key: supplier + supplier_item_name."""
	name = (supplier_item_name or "").strip()
	code = (supplier_item_code or "").strip()

	existing = None
	if name:
		existing = frappe.db.get_value(
			"eFactura Supplier Item Map",
			{"supplier": supplier, "supplier_item_name": name},
			"name",
			order_by="modified desc",
		)
	if not existing and code:
		existing = frappe.db.get_value(
			"eFactura Supplier Item Map",
			{"supplier": supplier, "supplier_item_code": code},
			"name",
		)

	if existing:
		doc = frappe.get_doc("eFactura Supplier Item Map", existing)
		doc.item_code = item_code
		doc.supplier_item_name = name or doc.supplier_item_name
		if code:
			doc.supplier_item_code = code
		if uom:
			doc.uom = uom
		doc.save(ignore_permissions=True)
		return doc.name

	doc = frappe.get_doc(
		{
			"doctype": "eFactura Supplier Item Map",
			"supplier": supplier,
			"supplier_item_code": code,
			"supplier_item_name": name,
			"item_code": item_code,
			"uom": uom,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name
