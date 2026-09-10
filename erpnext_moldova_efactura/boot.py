from erpnext_moldova_efactura.utils.factura_ai import paper_ai_enabled


def extend_bootinfo(bootinfo):
	bootinfo["moldova_efactura_paper_ai"] = 1 if paper_ai_enabled() else 0
