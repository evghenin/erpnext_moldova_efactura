from frappe import _


def get_data():
	return {
		"fieldname": "purchase_efactura",
		"internal_links": {
			"Purchase Invoice": ["items", "purchase_invoice"],
			"Purchase Receipt": ["items", "purchase_receipt"],
			"Delivery Note": ["items", "delivery_note"],
		},
		"transactions": [
			{"label": _("Buying"), "items": ["Purchase Order", "Purchase Receipt"]},
			{"label": _("Accounting"), "items": ["Purchase Invoice"]},
			{"label": _("Stock"), "items": ["Delivery Note"]},
		],
	}
