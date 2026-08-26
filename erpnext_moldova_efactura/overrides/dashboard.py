from frappe import _


def get_sales_invoice_dashboard(data):
    data['transactions'].append({
        'label': 'Invoicing',
        'items': ['Sales eFactura']
    })
    
    if not data.get('internal_links'):
        data['internal_links'] = {}
    
    data['internal_links']['Sales eFactura'] = ['items', 'sales_invoice']
    
    return data

def get_delivery_note_dashboard(data):
    data['transactions'].append({
        'label': 'Invoicing',
        'items': ['Sales eFactura']
    })
    
    if not data.get('internal_links'):
        data['internal_links'] = {}
    
    data['internal_links']['Sales eFactura'] = ['items', 'delivery_note']
    data["internal_links"]["Purchase eFactura"] = "purchase_efactura"
    data.setdefault("transactions", [])
    data["transactions"].append({"label": _("Purchase eFactura"), "items": ["Purchase eFactura"]})
    
    return data


def get_purchase_invoice_dashboard(data):
    data.setdefault("internal_links", {})
    data["internal_links"]["Purchase eFactura"] = "purchase_efactura"
    data.setdefault("transactions", [])
    data["transactions"].append({"label": _("Purchase eFactura"), "items": ["Purchase eFactura"]})
    return data


def get_purchase_order_dashboard(data):
    data.setdefault("internal_links", {})
    data["internal_links"]["Purchase eFactura"] = "purchase_efactura"
    data.setdefault("transactions", [])
    data["transactions"].append({"label": _("Purchase eFactura"), "items": ["Purchase eFactura"]})
    return data


def get_purchase_receipt_dashboard(data):
    data.setdefault("internal_links", {})
    data["internal_links"]["Purchase eFactura"] = "purchase_efactura"
    data.setdefault("transactions", [])
    data["transactions"].append({"label": _("Purchase eFactura"), "items": ["Purchase eFactura"]})
    return data


def get_sales_order_dashboard(data):
    data.setdefault("internal_links", {})
    data["internal_links"]["Sales eFactura"] = "sales_efactura"
    data.setdefault("transactions", [])
    data["transactions"].append({"label": _("Sales eFactura"), "items": ["Sales eFactura"]})
    return data