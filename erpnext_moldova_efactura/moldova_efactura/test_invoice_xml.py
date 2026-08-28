import unittest

from erpnext_moldova_efactura.utils.invoice_xml import parse_invoice_xml


class TestInvoiceXML(unittest.TestCase):
    def test_vat_total_falls_back_to_line_sum(self):
        # Header TotalTVA is missing/empty, but rows contain TotalTVA
        xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <Invoice>
            <SupplierInfo>
                <Merchandises>
                    <Row Quantity="1" UnitPriceWithoutTVA="100" TotalPrice="100" TotalPriceWithoutTVA="80" TotalTVA="20" Code="A" Name="ItemA" UnitOfMeasure="pcs" />
                    <Row Quantity="2" UnitPriceWithoutTVA="50" TotalPrice="110" TotalPriceWithoutTVA="100" TotalTVA="10" Code="B" Name="ItemB" UnitOfMeasure="pcs" />
                </Merchandises>
                <Total>210</Total>
                <TotalTVA></TotalTVA>
            </SupplierInfo>
        </Invoice>'''

        parsed = parse_invoice_xml(xml)
        # VAT total should equal 20 + 10 = 30
        self.assertAlmostEqual(parsed.get("vat_total", 0), 30)


if __name__ == "__main__":
    unittest.main()
