from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPoIaImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env["po.ia.import.wizard"]
        cls.Product = cls.env["product.product"]
        cls.Supplier = cls.env["product.supplierinfo"]

        # CUIT válido (checksum correcto) para el proveedor de prueba.
        cls.cuit = "30-50000000-3"
        cls.partner = cls.env["res.partner"].create({
            "name": "PROV TEST SA",
            "company_type": "company",
            "vat": cls.cuit,
        })
        # Producto con default_code propio (capa 2 del match).
        cls.prod_dc = cls.Product.create({
            "name": "Cubierta DC Test",
            "default_code": "DC001",
            "list_price": 5000.0,
            "purchase_ok": True,
        })
        # Producto que se asigna a mano (sin código que matchee).
        cls.prod_manual = cls.Product.create({
            "name": "Cubierta Manual Test ZZZ",
            "list_price": 7000.0,
            "purchase_ok": True,
        })

    def _new_wizard(self):
        # pdf_file con cualquier contenido: la extracción está mockeada.
        return self.Wizard.create({
            "pdf_file": "JVBERi0xLjQK",  # "%PDF-1.4" en base64
            "pdf_filename": "test.pdf",
        })

    def _fake_extraction(self, lineas, cuit=None):
        return {
            "proveedor": {"cuit": cuit or self.cuit, "razon_social": "PROV TEST SA"},
            "referencia": "PRES-001",
            "fecha": "2026-06-17",
            "moneda": self.env.company.currency_id.name,
            "lineas": lineas,
            "confianza": 0.9,
            "notas": "",
            "_usage": {"input_tokens": 10, "output_tokens": 20},
        }

    def _run_extract(self, wiz, fake):
        with patch.object(type(self.env["ia.haiku.service"]),
                          "extract_purchase_quote", return_value=fake):
            wiz.action_extract()

    # ------------------------------------------------------------------
    # Match por capas
    # ------------------------------------------------------------------
    def test_match_default_code(self):
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "DC001", "lo que sea")
        self.assertEqual(prod, self.prod_dc)
        self.assertEqual(status, "auto_code")

    def test_match_supplierinfo_wins(self):
        # Sembramos supplierinfo: el código del proveedor apunta a prod_manual.
        self.Supplier.create({
            "partner_id": self.partner.id,
            "product_tmpl_id": self.prod_manual.product_tmpl_id.id,
            "product_code": "PRV-9",
            "price": 100.0,
        })
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "PRV-9", "desc")
        self.assertEqual(prod, self.prod_manual)
        self.assertEqual(status, "auto_supplier")

    def test_match_name_suggested(self):
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "", "Cubierta Manual Test ZZZ")
        self.assertEqual(prod, self.prod_manual)
        self.assertEqual(status, "suggested")

    def test_match_none(self):
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "NOEXISTE", "xxxxx inexistente yyyy")
        self.assertFalse(prod)
        self.assertEqual(status, "none")

    # ------------------------------------------------------------------
    # Flujo completo
    # ------------------------------------------------------------------
    def test_create_po_forces_price_and_seeds_supplierinfo(self):
        wiz = self._new_wizard()
        fake = self._fake_extraction([
            {"codigo": "DC001", "descripcion": "Cubierta DC Test",
             "cantidad": 2, "precio_unit": 1234.5, "descuento": 0},
            {"codigo": "PRV-X", "descripcion": "renglón a mano",
             "cantidad": 1, "precio_unit": 999.0, "descuento": 0},
        ])
        self._run_extract(wiz, fake)

        self.assertEqual(wiz.state, "review")
        self.assertEqual(wiz.partner_id, self.partner)
        self.assertEqual(len(wiz.line_ids), 2)

        line1 = wiz.line_ids.filtered(lambda l: l.codigo == "DC001")
        self.assertEqual(line1.product_id, self.prod_dc)
        line2 = wiz.line_ids.filtered(lambda l: l.codigo == "PRV-X")
        self.assertFalse(line2.product_id)  # sin match → se asigna a mano
        line2.product_id = self.prod_manual

        action = wiz.action_create_po()
        order = self.env["purchase.order"].browse(action["res_id"])

        # Presupuesto en borrador (nunca confirmado).
        self.assertEqual(order.state, "draft")
        self.assertEqual(len(order.order_line), 2)

        # price_unit respeta el PDF (no el list_price del producto).
        ol1 = order.order_line.filtered(lambda l: l.product_id == self.prod_dc)
        self.assertAlmostEqual(ol1.price_unit, 1234.5, 2)
        self.assertAlmostEqual(ol1.product_qty, 2.0, 2)

        # Sembró supplierinfo con el código del proveedor.
        si = self.Supplier.search([
            ("partner_id", "=", self.partner.id),
            ("product_code", "=", "DC001"),
        ])
        self.assertTrue(si)
        self.assertAlmostEqual(si.price, 1234.5, 2)

        # Re-leer el mismo PDF: ahora DC001 matchea por supplierinfo o código.
        wiz2 = self._new_wizard()
        self._run_extract(wiz2, fake)
        l2 = wiz2.line_ids.filtered(lambda l: l.codigo == "PRV-X")
        self.assertEqual(l2.product_id, self.prod_manual)
        self.assertEqual(l2.match_status, "auto_supplier")

    def test_create_blocks_without_partner(self):
        wiz = self._new_wizard()
        fake = self._fake_extraction(
            [{"codigo": "DC001", "descripcion": "x", "cantidad": 1,
              "precio_unit": 10, "descuento": 0}],
            cuit="20-00000000-0",  # CUIT que no existe en la base
        )
        self._run_extract(wiz, fake)
        self.assertFalse(wiz.partner_id)
        with self.assertRaises(UserError):
            wiz.action_create_po()

    def test_seed_supplierinfo_dedupe(self):
        wiz = self._new_wizard()
        wiz.partner_id = self.partner
        # Sembramos dos veces el mismo (partner, tmpl, código): no duplica, actualiza.
        line = self.env["po.ia.import.wizard.line"].create({
            "wizard_id": wiz.id, "codigo": "DUP1",
            "descripcion": "d", "cantidad": 1, "precio_unit": 50.0,
            "product_id": self.prod_dc.id,
        })
        wiz._seed_supplierinfo(line)
        line.precio_unit = 80.0
        wiz._seed_supplierinfo(line)
        si = self.Supplier.search([
            ("partner_id", "=", self.partner.id),
            ("product_code", "=", "DUP1"),
        ])
        self.assertEqual(len(si), 1)
        self.assertAlmostEqual(si.price, 80.0, 2)
