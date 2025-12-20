from frappe import _

def get_data():
    return {
        "fieldname": "efactura",
        "internal_links": {
            "Delivery Note": ["items", "delivery_note"],
            "Sales Invoice": ["items", "sales_invoice"],
        },
        "transactions": [{"label": _("Reference"), "items": ["Sales Invoice", "Delivery Note"]}],
    }