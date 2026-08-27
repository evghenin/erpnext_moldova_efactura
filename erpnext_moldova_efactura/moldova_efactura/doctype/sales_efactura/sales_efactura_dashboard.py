from frappe import _


def get_data():
	return {
		"fieldname": "sales_efactura",
		"internal_links": {
			"Delivery Note": ["items", "delivery_note"],
			"Sales Invoice": ["items", "sales_invoice"],
			"Purchase Receipt": ["items", "purchase_receipt"],
		},
		"transactions": [
			{"label": _("Reference"), "items": ["Sales Invoice", "Delivery Note", "Purchase Receipt"]},
		],
	}
