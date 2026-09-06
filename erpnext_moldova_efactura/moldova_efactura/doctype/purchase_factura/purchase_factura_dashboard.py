from frappe import _


def get_data():
	return {
		"fieldname": "purchase_factura",
		"internal_links": {"Purchase Invoice": "purchase_invoice"},
		"transactions": [{"label": _("Accounting"), "items": ["Purchase Invoice"]}],
	}
