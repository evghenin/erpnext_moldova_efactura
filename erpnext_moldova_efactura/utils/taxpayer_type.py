"""SFS TaxpayerType codes ↔ Select values on e-Factura documents."""

from __future__ import annotations

# SFS GetTaxpayersInfo / XML TaxpayerType:
# 1 — Juridic, 2 — Persoană fizică, 3 — Nerezident
# Stored Select values are English. Translate "Company" only with DocType context
# (Purchase eFactura / Sales eFactura) so ERPNext's global Company label is unchanged.
CODE_TO_LABEL = {
	"1": "Company",
	"2": "Individual",
	"3": "Non-Resident",
}
LABEL_TO_CODE = {label: code for code, label in CODE_TO_LABEL.items()}
SELECT_OPTIONS = "\n".join(["", *CODE_TO_LABEL.values()])


def taxpayer_type_from_sfs(value) -> str:
	"""Map API/XML code (1/2/3) to Select value. Pass through if already a label."""
	if value is None:
		return ""
	raw = str(value).strip()
	if not raw:
		return ""
	if raw in LABEL_TO_CODE:
		return raw
	return CODE_TO_LABEL.get(raw, raw)


def taxpayer_type_to_sfs(value) -> str:
	"""Map Select value to XML TaxpayerType code."""
	if value is None:
		return ""
	raw = str(value).strip()
	if not raw:
		return ""
	if raw in CODE_TO_LABEL:
		return raw
	return LABEL_TO_CODE.get(raw, raw)
