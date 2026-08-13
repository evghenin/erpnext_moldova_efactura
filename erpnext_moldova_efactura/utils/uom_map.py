"""Supplier eFactura UOM ↔ ERPNext UOM mapping and qty conversion."""

from __future__ import annotations

import frappe
from frappe.utils import flt

_uom_alias_cache: dict[str, str] | None = None


def _settings():
	return frappe.get_single("eFactura Settings")


def _match_languages() -> list[str]:
	"""eFactura MD uses Romanian UOM labels; also try Settings language."""
	langs = ["ro"]
	settings_lang = frappe.db.get_single_value("eFactura Settings", "language")
	if settings_lang and settings_lang not in langs:
		langs.append(settings_lang)
	return langs


def _norm(value: str | None) -> str:
	return (value or "").strip().lower()


def _translated(source: str | None, language: str) -> str:
	if not source:
		return ""
	translated = frappe.db.get_value(
		"Translation",
		{"language": language, "source_text": source},
		"translated_text",
	)
	if translated:
		return translated
	try:
		return frappe._(source, language)
	except Exception:
		return source


def _build_uom_alias_index() -> dict[str, str]:
	"""Map normalized alias → UOM.name (exact 1:1 candidates)."""
	global _uom_alias_cache
	if _uom_alias_cache is not None:
		return _uom_alias_cache

	index: dict[str, str] = {}
	langs = _match_languages()
	uoms = frappe.get_all("UOM", fields=["name", "print_name"], limit_page_length=0)
	uom_by_name = {u.name: u.name for u in uoms if u.name}
	uom_by_print = {_norm(u.print_name): u.name for u in uoms if u.print_name}
	sources = set(uom_by_name) | {u.print_name for u in uoms if u.print_name}

	def _put(alias: str, uom_name: str):
		key = _norm(alias)
		if not key or key in index:
			return
		index[key] = uom_name

	for uom in uoms:
		name = uom.name or ""
		print_name = uom.print_name or ""
		_put(name, name)
		_put(print_name, name)
		for lang in langs:
			_put(_translated(name, lang), name)
			_put(_translated(print_name, lang), name)

	if sources:
		for lang in langs:
			for row in frappe.get_all(
				"Translation",
				filters={"language": lang, "source_text": ["in", list(sources)]},
				fields=["source_text", "translated_text"],
				limit_page_length=0,
			):
				src = row.source_text or ""
				alias = row.translated_text or ""
				if not alias:
					continue
				uom_name = uom_by_name.get(src) or uom_by_print.get(_norm(src))
				if uom_name:
					_put(alias, uom_name)

	_uom_alias_cache = index
	return index


def clear_uom_alias_cache():
	global _uom_alias_cache
	_uom_alias_cache = None


def resolve_uom_from_settings_map(supplier_uom: str | None) -> str | None:
	"""Settings → UOM Map only."""
	if not supplier_uom:
		return None
	key_norm = _norm(str(supplier_uom))
	if not key_norm:
		return None
	for row in _settings().get("uom_map") or []:
		if _norm(row.supplier_uom) == key_norm and row.uom:
			return row.uom
	return None


def resolve_uom_from_system(supplier_uom: str | None) -> str | None:
	"""UOM.name / print_name + translations (no Settings map)."""
	if not supplier_uom:
		return None
	key = str(supplier_uom).strip()
	if not key:
		return None
	if frappe.db.exists("UOM", key):
		return key
	return _build_uom_alias_index().get(_norm(key))


def resolve_uom(supplier_uom: str | None) -> str | None:
	"""
	Return ERP UOM for a supplier eFactura UOM string.

	Order:
	1) Settings UOM Map
	2) System UOM (name, print_name, translations)
	"""
	return resolve_uom_from_settings_map(supplier_uom) or resolve_uom_from_system(supplier_uom)


def auto_add_enabled() -> bool:
	return bool(frappe.db.get_single_value("eFactura Settings", "auto_add_uom_map_on_invoice_mapping"))


def ensure_uom_map(supplier_uom: str | None, uom: str | None) -> bool:
	"""Add mapping to Settings if missing and auto-add is enabled."""
	if not auto_add_enabled() or not supplier_uom or not uom:
		return False

	key = str(supplier_uom).strip()
	if not key or not frappe.db.exists("UOM", uom):
		return False

	settings = _settings()
	key_norm = _norm(key)
	for row in settings.get("uom_map") or []:
		if _norm(row.supplier_uom) == key_norm:
			return False

	settings.append("uom_map", {"supplier_uom": key, "uom": uom})
	settings.save(ignore_permissions=True)
	clear_uom_alias_cache()
	return True


def get_item_uom_conversion(item_code: str, uom: str) -> float:
	"""Conversion factor: how many stock_uom in one `uom` (from Item UOM table)."""
	if not item_code or not uom:
		return 1.0
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	if not stock_uom or stock_uom == uom:
		return 1.0
	try:
		from erpnext.stock.get_item_details import get_conversion_factor

		out = get_conversion_factor(item_code, uom)
		if isinstance(out, dict):
			return flt(out.get("conversion_factor")) or 1.0
		return flt(out) or 1.0
	except Exception:
		conv = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item_code, "uom": uom},
			"conversion_factor",
		)
		return flt(conv) or 1.0


def compute_buyer_item_qtys(
	item_code: str | None = None,
	ef_uom: str | None = None,
	ef_qty: float | None = None,
	uom: str | None = None,
) -> dict:
	"""
	Compute stock_uom / stock_qty / qty from eFactura qty and Item UOM conversions.

	stock_qty = ef_qty * (ef_uom → stock_uom)
	qty       = stock_qty / (uom → stock_uom)
	"""
	ef_qty = flt(ef_qty)
	result = {
		"stock_uom": None,
		"stock_qty": 0.0,
		"qty": ef_qty,
	}

	if not item_code:
		if ef_uom:
			result["stock_qty"] = ef_qty
		return result

	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	result["stock_uom"] = stock_uom

	if not ef_uom:
		return result

	ef_cf = get_item_uom_conversion(item_code, ef_uom) or 1.0
	stock_qty = ef_qty * flt(ef_cf)
	result["stock_qty"] = stock_qty

	if uom:
		uom_cf = get_item_uom_conversion(item_code, uom) or 1.0
		result["qty"] = stock_qty / flt(uom_cf) if uom_cf else stock_qty
	else:
		result["qty"] = ef_qty

	return result


def _item_purchase_and_stock_uom(item_code: str) -> tuple[str | None, str | None]:
	vals = frappe.db.get_value("Item", item_code, ["purchase_uom", "stock_uom"], as_dict=True)
	if not vals:
		return None, None
	return vals.purchase_uom or None, vals.stock_uom or None


def apply_billing_uom(row) -> None:
	"""
	Resolve ef_uom from supplier_uom text.

	- Settings map, then system UOM search
	- On match: set ef_uom; if uom empty, set uom = ef_uom
	- On no match: leave ef_uom empty unless a valid one is already set
	"""
	raw = getattr(row, "supplier_uom", None)
	legacy = getattr(row, "ef_uom", None)
	if legacy and not frappe.db.exists("UOM", legacy) and not raw:
		row.supplier_uom = legacy
		row.ef_uom = None

	# Keep an already valid ef_uom; still back-fill empty uom from it
	if row.ef_uom and frappe.db.exists("UOM", row.ef_uom):
		if not row.uom:
			row.uom = row.ef_uom
		return

	matched = resolve_uom(getattr(row, "supplier_uom", None))
	if matched:
		row.ef_uom = matched
		if not row.uom:
			row.uom = matched
	else:
		row.ef_uom = None


def apply_qty_defaults(row, force: bool = False) -> None:
	"""
	Derive stock_uom/stock_qty and PI qty from eFactura qty + Item UOM conversions.

	If supplier UOM (e.g. buc) is unknown, once item_code is mapped fall back to
	the Item Stock UOM so the mapper does not leave eFactura UOM empty.
	"""
	apply_billing_uom(row)

	if row.item_code and not row.ef_uom:
		_purchase_uom, stock_uom = _item_purchase_and_stock_uom(row.item_code)
		row.ef_uom = stock_uom

	if row.item_code and not row.uom:
		purchase_uom, stock_uom = _item_purchase_and_stock_uom(row.item_code)
		row.uom = row.ef_uom or purchase_uom or stock_uom

	computed = compute_buyer_item_qtys(
		item_code=row.item_code,
		ef_uom=row.ef_uom,
		ef_qty=row.ef_qty,
		uom=row.uom,
	)
	row.stock_uom = computed["stock_uom"]
	row.stock_qty = computed["stock_qty"]
	row.qty = computed["qty"]


def apply_booking_defaults(row, force: bool = False) -> None:
	"""Backward-compatible alias."""
	apply_qty_defaults(row, force=force)


def apply_uom_to_buyer_row(row) -> None:
	"""Entrypoint used by buyer controller."""
	apply_billing_uom(row)
	apply_qty_defaults(row, force=False)
