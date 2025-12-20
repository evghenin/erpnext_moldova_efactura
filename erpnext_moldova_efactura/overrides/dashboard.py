def get_sales_invoice_dashboard(data):
    data['transactions'].append({
        'label': 'Invoicing',
        'items': ['eFactura']
    })
    
    if not data.get('internal_links'):
        data['internal_links'] = {}
    
    data['internal_links']['eFactura'] = ['items', 'sales_invoice']
    
    return data

def get_delivery_note_dashboard(data):
    data['transactions'].append({
        'label': 'Invoicing',
        'items': ['eFactura']
    })
    
    if not data.get('internal_links'):
        data['internal_links'] = {}
    
    data['internal_links']['eFactura'] = ['items', 'delivery_note']
    
    return data