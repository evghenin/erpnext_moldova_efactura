"""Cache GetTaxpayersInfo rows by fiscal code."""

from __future__ import annotations

import copy
from datetime import timedelta

import frappe

from erpnext_moldova_efactura.utils.api_response import as_list

CACHE_KEY_PREFIX = "efactura_taxpayer:"
CACHE_TTL_DAYS = 120
CACHE_TTL_SECONDS = CACHE_TTL_DAYS * 24 * 60 * 60


def cache_key(fiscal_code: str) -> str:
	return f"{CACHE_KEY_PREFIX}{fiscal_code}"


def normalize_fiscal_codes(fiscal_codes) -> list[str]:
	codes = []
	for code in fiscal_codes or []:
		text = str(code or "").strip()
		if text:
			codes.append(text)
	return codes


def lookup_taxpayers(fiscal_codes: list[str]) -> tuple[dict[str, dict], list[str]]:
	"""Return cached taxpayer rows and the codes that still need SFS."""
	found: dict[str, dict] = {}
	missing: list[str] = []
	seen_missing: set[str] = set()
	for code in fiscal_codes:
		if code in found or code in seen_missing:
			continue
		taxpayer = read_taxpayer(code)
		if taxpayer is None:
			missing.append(code)
			seen_missing.add(code)
		else:
			found[code] = taxpayer
	return found, missing


def read_taxpayer(fiscal_code: str) -> dict | None:
	payload = frappe.cache.get_value(cache_key(fiscal_code), expires=True)
	if not isinstance(payload, dict):
		return None
	fetched_on = payload.get("fetched_on")
	taxpayer = payload.get("taxpayer")
	if not isinstance(taxpayer, dict) or not _is_fresh(fetched_on):
		frappe.cache.delete_value(cache_key(fiscal_code))
		return None
	return copy.deepcopy(taxpayer)


def store_taxpayers(response: dict | None) -> dict[str, dict]:
	"""Store Taxpayer rows from a successful GetTaxpayersInfo response."""
	results = (response or {}).get("Results") or {}
	rows = results.get("Taxpayer") if isinstance(results, dict) else None
	stored: dict[str, dict] = {}
	for row in as_list(rows):
		if not isinstance(row, dict):
			continue
		fiscal_code = str(row.get("IDNO") or "").strip()
		if not fiscal_code:
			continue
		taxpayer = copy.deepcopy(row)
		_write(fiscal_code, taxpayer)
		stored[fiscal_code] = taxpayer
	return stored


def taxpayer_response(fiscal_codes: list[str], taxpayers: dict[str, dict], request_id: str) -> dict:
	rows = []
	for code in fiscal_codes:
		row = taxpayers.get(code)
		if row:
			rows.append(copy.deepcopy(row))
	return {"RequestId": request_id, "Results": {"Taxpayer": rows}}


def clear_taxpayer_cache() -> int:
	keys = frappe.cache.get_keys(CACHE_KEY_PREFIX) or []
	if keys:
		frappe.cache.delete_value(keys, make_keys=False)
	return len(keys)


def _write(fiscal_code: str, taxpayer: dict, fetched_on=None) -> None:
	frappe.cache.set_value(
		cache_key(fiscal_code),
		{"fetched_on": fetched_on or frappe.utils.now_datetime(), "taxpayer": taxpayer},
		expires_in_sec=CACHE_TTL_SECONDS,
	)


def _is_fresh(fetched_on) -> bool:
	if fetched_on is None or not hasattr(fetched_on, "year"):
		return False
	try:
		age = frappe.utils.now_datetime() - fetched_on
	except TypeError:
		return False
	return age < timedelta(days=CACHE_TTL_DAYS)
