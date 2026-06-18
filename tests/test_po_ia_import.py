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

    def test_match_prefix_underscore(self):
        # Código tipo Michelin "<nuestro_code>_<nro>" → matchea por prefijo exacto.
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "DC001_7", "lo que sea")
        self.assertEqual(prod, self.prod_dc)
        self.assertEqual(status, "auto_prefix")

    def test_match_prefix_no_false_positive(self):
        # Prefijo que no es un default_code existente → no inventa match.
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "NOEXISTE_9", "xxxx inexistente yyyy")
        self.assertFalse(prod)
        self.assertEqual(status, "none")

    def test_match_none(self):
        wiz = self._new_wizard()
        prod, status = wiz._match_product(self.partner, "NOEXISTE", "xxxxx inexistente yyyy")
        self.assertFalse(prod)
        self.assertEqual(status, "none")

    def test_match_leading_zeros(self):
        # default_code con ceros a la izquierda; el PDF trae el código sin ellos.
        prod = self.Product.create({
            "name": "Cubierta Z Test", "default_code": "000060", "purchase_ok": True})
        wiz = self._new_wizard()
        p, status = wiz._match_product(self.partner, "60", "zzz inexistente")
        self.assertEqual(p, prod)
        self.assertEqual(status, "auto_code")

    def test_match_leading_zeros_ambiguous(self):
        # Dos productos colapsan al mismo código normalizado → no adivina.
        self.Product.create({"name": "Amb A", "default_code": "0070", "purchase_ok": True})
        self.Product.create({"name": "Amb B", "default_code": "70", "purchase_ok": True})
        wiz = self._new_wizard()
        p, status = wiz._match_product(self.partner, "070", "qqq inexistente www")
        self.assertFalse(p)
        self.assertEqual(status, "none")

    # ------------------------------------------------------------------
    # Descuento (importe → %)
    # ------------------------------------------------------------------
    def test_discount_from_importe(self):
        wiz = self._new_wizard()
        # Michelin fila 1: 449241.58 × 8 = 3.593.932,64; importe 2.443.874,20 → 32%.
        pct = wiz._discount_pct(449241.58, 8, 2443874.20, 0)
        self.assertAlmostEqual(pct, 32.0, 1)

    def test_discount_fallback_and_incoherent(self):
        wiz = self._new_wizard()
        # Sin importe → usa el % de la IA si es válido.
        self.assertEqual(wiz._discount_pct(100, 2, 0, 15), 15)
        # importe >= bruto (incoherente) → ignora importe, cae al fallback (0).
        self.assertEqual(wiz._discount_pct(100, 2, 999, 0), 0)

    # ------------------------------------------------------------------
    # Percepciones (mapeo a impuesto nativo de compra)
    # ------------------------------------------------------------------
    def test_perception_mapping(self):
        tax = self.env["account.tax"].create({
            "name": "P. IIBB MZA 3%", "type_tax_use": "purchase",
            "amount_type": "percent", "amount": 3.0})
        wiz = self._new_wizard()
        cmds = wiz._build_perceptions([
            {"tipo": "IIBB", "jurisdiccion": "Mendoza", "alicuota": 3.0, "importe": 203701.30}])
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][2]["tax_id"], tax.id)
        self.assertAlmostEqual(cmds[0][2]["importe"], 203701.30, 2)

    def test_perception_unmatched(self):
        wiz = self._new_wizard()
        cmds = wiz._build_perceptions([
            {"tipo": "IIBB", "jurisdiccion": "Neuquén", "alicuota": 7.77, "importe": 100.0}])
        self.assertFalse(cmds[0][2]["tax_id"])  # sin tax que matchee → no inventa

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

    def test_create_po_attaches_source_pdf(self):
        wiz = self._new_wizard()
        fake = self._fake_extraction([
            {"codigo": "DC001", "descripcion": "Cubierta DC Test",
             "cantidad": 1, "precio_unit": 100.0, "descuento": 0}])
        self._run_extract(wiz, fake)
        action = wiz.action_create_po()
        order = self.env["purchase.order"].browse(action["res_id"])
        att = self.env["ir.attachment"].search([
            ("res_model", "=", "purchase.order"), ("res_id", "=", order.id)])
        self.assertTrue(att, "El PDF origen debe quedar adjunto al PO")
        self.assertEqual(att[0].name, "test.pdf")
        self.assertTrue(
            order.message_ids.filtered(lambda m: "Haiku" in (m.body or "")),
            "Debe haber una nota en el chatter referenciando el archivo origen")

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


@tagged("post_install", "-at_install")
class TestPoIaProductCreate(TransactionCase):
    """Sub-wizard de alta de producto/variante desde el macheo (modelo Camiletti:
    plantilla = Medida, atributos = Marca/Modelo, variantes dinámicas)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Attr = cls.env["product.attribute"]
        cls.AttrVal = cls.env["product.attribute.value"]
        cls.Tmpl = cls.env["product.template"]
        cls.CreateWiz = cls.env["po.ia.product.create.wizard"]

        cls.marca = cls.Attr.create({"name": "Marca", "create_variant": "dynamic"})
        cls.modelo = cls.Attr.create({"name": "Modelo", "create_variant": "dynamic"})
        cls.pirelli = cls.AttrVal.create({"name": "Pirelli", "attribute_id": cls.marca.id})
        cls.prince = cls.AttrVal.create({"name": "Prince", "attribute_id": cls.modelo.id})

        # Plantilla (medida) existente con Marca=Pirelli, Modelo=Prince.
        cls.medida = cls.Tmpl.create({
            "name": "225/75 R18",
            "purchase_ok": True,
            "attribute_line_ids": [
                (0, 0, {"attribute_id": cls.marca.id, "value_ids": [(6, 0, [cls.pirelli.id])]}),
                (0, 0, {"attribute_id": cls.modelo.id, "value_ids": [(6, 0, [cls.prince.id])]}),
            ],
        })

    def _line(self, codigo="", desc=""):
        wiz = self.env["po.ia.import.wizard"].create({
            "pdf_file": "JVBERi0xLjQK", "pdf_filename": "t.pdf"})
        return self.env["po.ia.import.wizard.line"].create({
            "wizard_id": wiz.id, "codigo": codigo, "descripcion": desc,
            "cantidad": 1, "precio_unit": 100.0,
        })

    def test_variant_with_new_attribute_value(self):
        """Sobre medida existente, Modelo nuevo 'Bis' → da de alta el valor,
        lo suma a la plantilla y genera la variante, asignándola a la línea."""
        line = self._line(codigo="NEW-CODE-1", desc="225/75 R18 Pirelli Bis")
        wiz = self.CreateWiz.create({
            "line_id": line.id,
            "product_tmpl_id": self.medida.id,
            "ref_code": "NEW-CODE-1",
            "attr_line_ids": [
                (0, 0, {"attribute_id": self.marca.id, "value_id": self.pirelli.id}),
                (0, 0, {"attribute_id": self.modelo.id, "new_value": "Bis"}),
            ],
        })
        wiz.action_apply()
        # El valor nuevo se creó y quedó en la plantilla.
        bis = self.AttrVal.search([("attribute_id", "=", self.modelo.id), ("name", "=", "Bis")])
        self.assertEqual(len(bis), 1)
        self.assertIn(bis, self.medida.attribute_line_ids.filtered(
            lambda l: l.attribute_id == self.modelo).value_ids)
        # La línea quedó con una variante de la medida y estado 'created'.
        self.assertTrue(line.product_id)
        self.assertEqual(line.product_id.product_tmpl_id, self.medida)
        self.assertEqual(line.match_status, "created")
        self.assertEqual(line.product_id.default_code, "NEW-CODE-1")

    def test_variant_reuses_existing_combination(self):
        """Misma combinación pedida dos veces → reutiliza la variante (no duplica)."""
        def apply():
            line = self._line(desc="x")
            wiz = self.CreateWiz.create({
                "line_id": line.id,
                "product_tmpl_id": self.medida.id,
                "attr_line_ids": [
                    (0, 0, {"attribute_id": self.marca.id, "value_id": self.pirelli.id}),
                    (0, 0, {"attribute_id": self.modelo.id, "value_id": self.prince.id}),
                ],
            })
            wiz.action_apply()
            return line.product_id
        v1 = apply()
        v2 = apply()
        self.assertEqual(v1, v2)

    def test_medida_nueva_sin_atributos_previos(self):
        """Medida creada al vuelo (plantilla sin atributos): se eligen atributos y
        valores en el momento y se genera la variante."""
        nueva = self.Tmpl.create({"name": "300/80 R22"})  # como el quick-create del m2o
        line = self._line(desc="medida nueva")
        wiz = self.CreateWiz.create({
            "line_id": line.id,
            "product_tmpl_id": nueva.id,
            "attr_line_ids": [
                (0, 0, {"attribute_id": self.marca.id, "value_id": self.pirelli.id}),
                (0, 0, {"attribute_id": self.modelo.id, "new_value": "Nuevo Modelo"}),
            ],
        })
        wiz.action_apply()
        self.assertTrue(line.product_id)
        self.assertEqual(line.product_id.product_tmpl_id, nueva)
        self.assertEqual(line.match_status, "created")
        self.assertTrue(nueva.purchase_ok)
