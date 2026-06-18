import logging
import re

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _norm_digits(value):
    """Devuelve solo los dígitos de un CUIT/VAT (para comparar con/sin guiones)."""
    return re.sub(r"\D", "", value or "")


def _strip_lead_zeros(value):
    """Saca los ceros a la izquierda para comparar códigos (000060 == 60).

    No toca ceros internos: '001.01.0129' -> '1.01.0129'. Si el código es todo
    ceros, devuelve el original (evita clave vacía)."""
    s = (value or "").strip()
    return s.lstrip("0") or s


class PoIaImportWizard(models.TransientModel):
    _name = "po.ia.import.wizard"
    _description = "Carga de presupuesto de compra desde PDF con IA (Haiku)"

    state = fields.Selection(
        [("upload", "Subir PDF"), ("review", "Revisar")],
        default="upload", string="Etapa",
    )
    pdf_file = fields.Binary(string="PDF del proveedor", attachment=False)
    pdf_filename = fields.Char(string="Nombre del archivo")

    # Cabecera detectada por la IA.
    partner_id = fields.Many2one("res.partner", string="Proveedor", domain="[('is_company','in',[True,False])]")
    detected_cuit = fields.Char(string="CUIT detectado", readonly=True)
    detected_name = fields.Char(string="Razón social detectada", readonly=True)
    partner_ref = fields.Char(string="Referencia del proveedor")
    date_order = fields.Date(string="Fecha")
    currency_id = fields.Many2one("res.currency", string="Moneda")

    line_ids = fields.One2many("po.ia.import.wizard.line", "wizard_id", string="Líneas")

    confidence = fields.Float(string="Confianza IA", readonly=True)
    notes = fields.Text(string="Notas de la IA", readonly=True)
    usage_info = fields.Char(string="Consumo", readonly=True)
    force_create = fields.Boolean(
        string="Crear igual (ya existe un RFQ similar)",
        help="Tildar para crear el presupuesto aunque ya exista uno en borrador "
             "del mismo proveedor con la misma referencia.",
    )

    # ------------------------------------------------------------------
    # Extracción
    # ------------------------------------------------------------------
    def action_extract(self):
        self.ensure_one()
        if not self.pdf_file:
            raise UserError(_("Subí primero el PDF del presupuesto."))

        data = self.env["ia.haiku.service"].extract_purchase_quote(
            self.pdf_file, self.pdf_filename or "presupuesto.pdf")

        prov = data.get("proveedor") or {}
        cuit = _norm_digits(prov.get("cuit"))
        partner = self._find_partner(cuit)

        usage = data.get("_usage") or {}
        vals = {
            "state": "review",
            "detected_cuit": prov.get("cuit") or "",
            "detected_name": prov.get("razon_social") or "",
            "partner_id": partner.id if partner else False,
            "partner_ref": data.get("referencia") or "",
            "date_order": data.get("fecha") or False,
            "currency_id": self._find_currency(data.get("moneda")).id or False,
            "confidence": data.get("confianza") or 0.0,
            "notes": data.get("notas") or "",
            "usage_info": "in=%s out=%s tokens" % (
                usage.get("input_tokens", "?"), usage.get("output_tokens", "?")),
            "line_ids": [(5, 0, 0)] + self._build_lines(partner, data.get("lineas") or []),
        }
        self.write(vals)
        return self._reopen()

    def _build_lines(self, partner, lineas):
        cmds = []
        code_index = self._build_code_index()
        for ln in lineas:
            codigo = (ln.get("codigo") or "").strip()
            desc = (ln.get("descripcion") or "").strip()
            cantidad = ln.get("cantidad") or 0.0
            precio_unit = ln.get("precio_unit") or 0.0
            importe = ln.get("importe") or 0.0
            descuento = self._discount_pct(precio_unit, cantidad, importe, ln.get("descuento") or 0.0)
            product, status = self._match_product(partner, codigo, desc, code_index=code_index)
            cmds.append((0, 0, {
                "codigo": codigo,
                "descripcion": desc,
                "cantidad": cantidad,
                "precio_unit": precio_unit,
                "descuento": descuento,
                "importe": importe,
                "product_id": product.id if product else False,
                "match_status": status,
            }))
        return cmds

    @staticmethod
    def _discount_pct(precio_unit, cantidad, importe, descuento_ia):
        """Devuelve el % de descuento de la línea.

        Si hay `importe` (total con descuento, sin IVA) y un bruto > 0, calcula el %
        exacto = (1 - importe/bruto)*100 — banca facturas que dan el descuento como
        monto (caso Michelin). Si no, usa el `descuento` de la IA cuando es un % válido.
        """
        bruto = (precio_unit or 0.0) * (cantidad or 0.0)
        if importe and bruto and 0 < importe < bruto:
            return round((1.0 - importe / bruto) * 100.0, 4)
        if 0.0 <= descuento_ia <= 100.0:
            return descuento_ia
        return 0.0

    # ------------------------------------------------------------------
    # Match contra el catálogo
    # ------------------------------------------------------------------
    def _build_code_index(self):
        """Índice {default_code sin ceros a la izquierda: [product ids]} para
        machear tolerando ceros a la izquierda. Se arma una vez por carga."""
        index = {}
        for p in self.env["product.product"].search_read(
                [("default_code", "!=", False)], ["default_code"]):
            index.setdefault(_strip_lead_zeros(p["default_code"]), []).append(p["id"])
        return index

    def _match_product(self, partner, codigo, descripcion, code_index=None):
        """Orden: supplierinfo → default_code → prefijo "_" → ceros a la izquierda → nombre.

        Devuelve (product.product|False, match_status).
        """
        Product = self.env["product.product"]
        # 1) supplierinfo del proveedor por su código (se va sembrando con el uso).
        if partner and codigo:
            si = self.env["product.supplierinfo"].search([
                ("partner_id", "=", partner.id),
                ("product_code", "=", codigo),
            ], limit=1)
            if si:
                prod = si.product_id or si.product_tmpl_id.product_variant_id
                if prod:
                    return prod, "auto_supplier"
        # 2) default_code propio (por si el proveedor usa nuestro código).
        if codigo:
            prod = Product.search([("default_code", "=", codigo)], limit=1)
            if prod:
                return prod, "auto_code"
        # 2.5) prefijo del código del proveedor: muchos códigos llegan como
        # "<nuestro_default_code>_<nro>" (caso Michelin). Cortamos el sufijo "_<nro>"
        # y buscamos default_code EXACTO del prefijo (no ilike → sin falsos positivos).
        if codigo and "_" in codigo:
            prefijos = []
            for pref in (codigo.rsplit("_", 1)[0], codigo.split("_", 1)[0]):
                pref = pref.strip()
                if pref and pref not in prefijos:
                    prefijos.append(pref)
            for pref in prefijos:
                prods = Product.search([("default_code", "=", pref)], limit=2)
                if len(prods) == 1:
                    return prods[0], "auto_prefix"
        # 2.7) ceros a la izquierda: normalizamos el código (y sus prefijos "_")
        # y comparamos contra el índice de default_code normalizados. Solo si la
        # versión normalizada da UN único producto (si varios colapsan, no adivinamos).
        if codigo:
            if code_index is None:
                code_index = self._build_code_index()
            cands = {_strip_lead_zeros(codigo)}
            if "_" in codigo:
                cands.add(_strip_lead_zeros(codigo.rsplit("_", 1)[0]))
                cands.add(_strip_lead_zeros(codigo.split("_", 1)[0]))
            for cand in cands:
                hits = code_index.get(cand) if cand else None
                if hits and len(hits) == 1:
                    return Product.browse(hits[0]), "auto_code"
        # 3) nombre ilike — match único de alta confianza.
        if descripcion:
            prods = Product.search(
                [("purchase_ok", "=", True), ("name", "ilike", descripcion)], limit=2)
            if len(prods) == 1:
                return prods[0], "suggested"
        return Product.browse(), "none"

    def _find_partner(self, cuit_digits):
        if not cuit_digits:
            return self.env["res.partner"].browse()
        # Compara por dígitos (banca el vat con o sin guiones).
        partners = self.env["res.partner"].search(
            [("vat", "!=", False), ("supplier_rank", ">", 0)])
        for p in partners:
            if _norm_digits(p.vat) == cuit_digits:
                return p
        # Reintento sin exigir supplier_rank (puede no estar marcado aún).
        partners = self.env["res.partner"].search([("vat", "!=", False)])
        for p in partners:
            if _norm_digits(p.vat) == cuit_digits:
                return p
        return self.env["res.partner"].browse()

    def _find_currency(self, code):
        code = (code or "").strip().upper()
        if not code:
            return self.env["res.currency"].browse()
        return self.env["res.currency"].search([("name", "=", code)], limit=1)

    # ------------------------------------------------------------------
    # Creación del presupuesto de compra
    # ------------------------------------------------------------------
    def action_create_po(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_(
                "No se reconoció el proveedor (CUIT detectado: %s). "
                "Seleccionalo a mano antes de crear el presupuesto.")
                % (self.detected_cuit or "—"))
        if not self.line_ids:
            raise UserError(_("No hay líneas para cargar."))
        sin_match = self.line_ids.filtered(lambda l: not l.product_id)
        if sin_match:
            raise UserError(_(
                "Hay %s línea(s) sin producto asignado. Completá el producto de "
                "cada línea (o borrá la línea) antes de crear el presupuesto.")
                % len(sin_match))

        self._check_duplicate()

        order = self.env["purchase.order"].create({
            "partner_id": self.partner_id.id,
            "partner_ref": self.partner_ref or False,
            "date_order": fields.Datetime.now(),
            "currency_id": self.currency_id.id or self.partner_id.property_purchase_currency_id.id or False,
        })
        # Crear líneas solo con product_id + cantidad; el price_unit se fuerza
        # después (campo computado-almacenado que Odoo pisa al crear — C.10/C.11).
        has_discount = "discount" in self.env["purchase.order.line"]._fields
        for wl in self.line_ids:
            line = self.env["purchase.order.line"].create({
                "order_id": order.id,
                "product_id": wl.product_id.id,
                "product_qty": wl.cantidad or 1.0,
            })
            write_vals = {"price_unit": wl.precio_unit or 0.0}
            if wl.descripcion:
                write_vals["name"] = wl.descripcion
            if has_discount and wl.descuento:
                write_vals["discount"] = wl.descuento
            line.write(write_vals)
            self._seed_supplierinfo(wl)

        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": order.id,
            "view_mode": "form",
            "target": "current",
        }

    def _check_duplicate(self):
        if self.force_create or not self.partner_ref:
            return
        dup = self.env["purchase.order"].search_count([
            ("partner_id", "=", self.partner_id.id),
            ("partner_ref", "=", self.partner_ref),
            ("state", "in", ("draft", "sent")),
        ])
        if dup:
            raise UserError(_(
                "Ya existe un presupuesto en borrador de %(prov)s con la referencia "
                "'%(ref)s'. Si querés cargarlo igual, tildá 'Crear igual'.",
                prov=self.partner_id.display_name, ref=self.partner_ref))

    def _seed_supplierinfo(self, wl):
        """Crea/actualiza product.supplierinfo con el código y precio del proveedor.

        Dedupe por (partner, product_tmpl, product_code) — B.7.
        """
        if not (wl.product_id and wl.codigo):
            return
        tmpl = wl.product_id.product_tmpl_id
        Supplier = self.env["product.supplierinfo"]
        existing = Supplier.search([
            ("partner_id", "=", self.partner_id.id),
            ("product_tmpl_id", "=", tmpl.id),
            ("product_code", "=", wl.codigo),
        ], limit=1)
        si_vals = {
            "product_name": wl.descripcion or False,
            "price": wl.precio_unit or 0.0,
        }
        if self.currency_id:
            si_vals["currency_id"] = self.currency_id.id
        if existing:
            existing.write(si_vals)
        else:
            Supplier.create({
                "partner_id": self.partner_id.id,
                "product_tmpl_id": tmpl.id,
                "product_id": wl.product_id.id if tmpl.product_variant_count > 1 else False,
                "product_code": wl.codigo,
                **si_vals,
            })

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class PoIaImportWizardLine(models.TransientModel):
    _name = "po.ia.import.wizard.line"
    _description = "Línea de presupuesto de compra leída por IA"

    wizard_id = fields.Many2one("po.ia.import.wizard", required=True, ondelete="cascade")
    codigo = fields.Char(string="Código proveedor")
    descripcion = fields.Char(string="Descripción (PDF)")
    cantidad = fields.Float(string="Cantidad", default=1.0)
    precio_unit = fields.Float(string="Precio unit.")
    descuento = fields.Float(string="Desc. %")
    importe = fields.Float(
        string="Importe (PDF)", readonly=True,
        help="Total de la línea según el PDF (con descuento, sin IVA). Referencia "
             "para verificar que la carga coincide con la factura.")
    product_id = fields.Many2one(
        "product.product", string="Producto",
        domain="[('purchase_ok','=',True)]")
    match_status = fields.Selection([
        ("auto_supplier", "Auto (proveedor)"),
        ("auto_code", "Auto (código)"),
        ("auto_prefix", "Auto (prefijo)"),
        ("suggested", "Sugerido"),
        ("created", "Creado"),
        ("none", "Sin match"),
    ], string="Match", default="none")

    def action_open_product_create(self):
        """Abre el sub-wizard para crear el producto/variante de esta línea."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Crear producto / variante"),
            "res_model": "po.ia.product.create.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_line_id": self.id,
                "default_src_codigo": self.codigo or "",
                "default_src_descripcion": self.descripcion or "",
            },
        }
