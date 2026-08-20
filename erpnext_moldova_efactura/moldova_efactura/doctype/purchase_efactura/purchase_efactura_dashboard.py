from frappe import _


def get_data():
	return {
		"fieldname": "purchase_efactura",
		"internal_links": {
			"Purchase Invoice": ["items", "purchase_invoice"],
		},
		"transactions": [
			{"label": _("Buying"), "items": ["Purchase Order"]},
			{"label": _("Accounting"), "items": ["Purchase Invoice"]},
		],
	}
