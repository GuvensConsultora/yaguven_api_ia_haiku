import base64
import hashlib
import json
import logging
import re

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import html_escape

_logger = logging.getLogger(__name__)

# Versión del esquema de extracción. Forma parte de la clave de caché: al
# cambiar el esquema/prompt (p. ej. agregar 'series'), se invalida la caché
# vieja y se vuelve a llamar a Haiku, en vez de devolver una extracción
# previa sin los campos nuevos. Bumpear ante cualquier cambio de esquema.
EXTRACTION_SCHEMA_VERSION = "2026-06-series"


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
    detected_condicion = fields.Char(string="Condición IVA (PDF)", readonly=True)
    detected_resp_id = fields.Many2one(
        "l10n_ar.afip.responsibility.type", string="Responsabilidad detectada", readonly=True)
    buyer_cuit = fields.Char(string="CUIT comprador (PDF)", readonly=True)
    company_dest_id = fields.Many2one(
        "res.company", string="Compañía destino", readonly=True,
        help="Compañía a la que corresponde la factura según el CUIT del comprador.")
    company_mismatch = fields.Boolean(compute="_compute_company_mismatch")

    @api.depends("company_dest_id")
    def _compute_company_mismatch(self):
        for wiz in self:
            wiz.company_mismatch = bool(
                wiz.company_dest_id and wiz.company_dest_id != self.env.company)
    partner_ref = fields.Char(string="Referencia del proveedor")
    date_order = fields.Date(string="Fecha")
    currency_id = fields.Many2one("res.currency", string="Moneda")

    line_ids = fields.One2many("po.ia.import.wizard.line", "wizard_id", string="Líneas")
    percepcion_ids = fields.One2many(
        "po.ia.import.wizard.perception", "wizard_id", string="Percepciones")

    # Totales del comprobante según el PDF (para comparar contra el PO en el chatter).
    pdf_neto = fields.Float(string="Neto (PDF)", readonly=True)
    pdf_iva = fields.Float(string="IVA (PDF)", readonly=True)
    pdf_perc = fields.Float(string="Percepciones (PDF)", readonly=True)
    pdf_total = fields.Float(string="Total (PDF)", readonly=True)

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

        # Caché por hash del PDF: si ya se leyó esta factura, se reutiliza (sin
        # volver a llamar a Haiku → sin costo). Útil al reabrir tras cambiar de cía.
        try:
            pdf_hash = hashlib.sha256(base64.b64decode(self.pdf_file)).hexdigest()
            # La versión del esquema entra en la clave: un cambio de esquema
            # invalida la caché vieja (evita devolver extracciones sin 'series').
            pdf_hash = "%s:%s" % (pdf_hash, EXTRACTION_SCHEMA_VERSION)
        except Exception:
            pdf_hash = None
        Cache = self.env["po.ia.extraction.cache"]
        cached = Cache.search([("pdf_hash", "=", pdf_hash)], limit=1) if pdf_hash else Cache.browse()
        if cached:
            data = json.loads(cached.result_json)
        else:
            data = self.env["ia.haiku.service"].extract_purchase_quote(
                self.pdf_file, self.pdf_filename or "presupuesto.pdf")
            if pdf_hash:
                Cache.create({"pdf_hash": pdf_hash, "result_json": json.dumps(data)})

        prov = data.get("proveedor") or {}
        cuit = _norm_digits(prov.get("cuit"))
        partner = self._find_partner(cuit)
        condicion = prov.get("condicion_iva") or ""
        resp = self._map_responsibility(condicion)

        # Comprador (nosotros) → compañía destino según su CUIT.
        comp = data.get("comprador") or {}
        buyer_cuit = _norm_digits(comp.get("cuit"))
        company_dest = self._find_company_by_cuit(buyer_cuit)

        usage = data.get("_usage") or {}
        tot = data.get("totales") or {}
        vals = {
            "state": "review",
            "pdf_neto": tot.get("neto") or 0.0,
            "pdf_iva": tot.get("iva") or 0.0,
            "pdf_perc": tot.get("percepciones") or 0.0,
            "pdf_total": tot.get("total") or 0.0,
            "detected_cuit": prov.get("cuit") or "",
            "detected_name": prov.get("razon_social") or "",
            "buyer_cuit": comp.get("cuit") or "",
            "company_dest_id": company_dest.id if company_dest else False,
            "detected_condicion": condicion,
            "detected_resp_id": resp.id if resp else False,
            "partner_id": partner.id if partner else False,
            "partner_ref": data.get("referencia") or "",
            "date_order": data.get("fecha") or False,
            "currency_id": self._find_currency(data.get("moneda")).id or False,
            "confidence": data.get("confianza") or 0.0,
            "notes": data.get("notas") or "",
            "usage_info": "in=%s out=%s tokens" % (
                usage.get("input_tokens", "?"), usage.get("output_tokens", "?")),
            "line_ids": [(5, 0, 0)] + self._build_lines(partner, data.get("lineas") or []),
            "percepcion_ids": [(5, 0, 0)] + self._build_perceptions(data.get("percepciones") or []),
        }
        self.write(vals)
        return self._reopen()

    def _build_perceptions(self, percepciones):
        company = self.env.company
        cmds = []
        for pc in percepciones:
            alicuota = pc.get("alicuota") or 0.0
            tipo = (pc.get("tipo") or "").strip()
            juris = (pc.get("jurisdiccion") or "").strip()
            tax = self._find_perception_tax(company, tipo, juris, alicuota)
            cmds.append((0, 0, {
                "tipo": tipo,
                "jurisdiccion": juris,
                "alicuota": alicuota,
                "importe": pc.get("importe") or 0.0,
                "tax_id": tax.id if tax else False,
            }))
        return cmds

    @staticmethod
    def _juris_tokens(jurisdiccion):
        """Tokens de nombre de impuesto para una jurisdicción (Mendoza -> MZA/MENDOZA)."""
        j = (jurisdiccion or "").strip().upper()
        alias = {
            "MENDOZA": ["MZA", "MENDOZA"],
            "BUENOS AIRES": ["PBA", "BUENOS AIRES"],
            "CABA": ["CABA"], "CIUDAD DE BUENOS AIRES": ["CABA"],
            "CORDOBA": ["CBA", "CORDOBA"], "CÓRDOBA": ["CBA", "CORDOBA"],
            "SANTA FE": ["SF", "SANTA FE"],
        }
        return alias.get(j, [j] if j else [])

    def _find_perception_tax(self, company, tipo, jurisdiccion, alicuota):
        """Busca el impuesto de compra (percepción) nativo que coincide en alícuota
        y jurisdicción. Devuelve account.tax vacío si no hay match inequívoco."""
        Tax = self.env["account.tax"]
        cands = Tax.search([
            ("type_tax_use", "=", "purchase"),
            ("company_id", "=", company.id),
            ("amount_type", "=", "percent"),
            ("amount", ">=", alicuota - 0.01),
            ("amount", "<=", alicuota + 0.01),
        ])
        if not cands:
            return Tax.browse()
        tokens = self._juris_tokens(jurisdiccion)
        if tokens:
            for t in cands:
                name = (t.name or "").upper()
                if any(tok in name for tok in tokens):
                    return t
        # Sin jurisdicción que matchee: solo si hay una única candidata por alícuota.
        return cands[0] if len(cands) == 1 else Tax.browse()

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
            series = ln.get("series") or []
            series_txt = "\n".join(
                s.strip() for s in series if isinstance(s, str) and s.strip())
            cmds.append((0, 0, {
                "codigo": codigo,
                "descripcion": desc,
                "cantidad": cantidad,
                "precio_unit": precio_unit,
                "descuento": descuento,
                "importe": importe,
                "product_id": product.id if product else False,
                "match_status": status,
                "series": series_txt,
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

    def _map_responsibility(self, condicion):
        """Mapea el texto de condición frente al IVA del PDF a la responsabilidad AFIP."""
        c = (condicion or "").lower()
        Resp = self.env["l10n_ar.afip.responsibility.type"]
        code = None
        if "monotrib" in c:
            code = "6"
        elif "exento" in c:
            code = "4"
        elif "no alcanz" in c:
            code = "15"
        elif "consumidor final" in c:
            code = "5"
        elif "responsable inscripto" in c or "resp. inscripto" in c or "resp inscripto" in c:
            code = "1"
        return Resp.search([("code", "=", code)], limit=1) if code else Resp.browse()

    # Mapeo responsabilidad -> grupo de impuesto IVA de compra a aplicar.
    # RI (1) y otros no listados: se respeta el IVA del producto (no se fuerza).
    _RESP_TO_VAT_GROUP = {
        "6": "VAT Not Applicable", "13": "VAT Not Applicable", "16": "VAT Not Applicable",
        "4": "VAT Exempt", "10": "VAT Exempt",
        "15": "VAT Not Applicable",
    }

    def _iva_tax_for_responsibility(self, company, resp):
        """Devuelve el IVA de compra a forzar según la responsabilidad (No Aplica para
        monotributo, Exento para exento) en la compañía dada. False para Resp. Inscripto
        (se respeta el IVA del producto)."""
        if not resp:
            return self.env["account.tax"].browse()
        group = self._RESP_TO_VAT_GROUP.get(resp.code)
        if not group:
            return self.env["account.tax"].browse()
        return self.env["account.tax"].search([
            ("type_tax_use", "=", "purchase"),
            ("company_id", "=", company.id),
            ("tax_group_id.name", "=", group),
        ], limit=1)

    def _find_company_by_cuit(self, cuit_digits):
        if not cuit_digits:
            return self.env["res.company"].browse()
        for c in self.env["res.company"].sudo().search([("vat", "!=", False)]):
            if _norm_digits(c.vat) == cuit_digits:
                return c
        return self.env["res.company"].browse()

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
        # La factura está dirigida a otra compañía: frenamos para que el usuario
        # cambie de compañía. La lectura quedó cacheada (no se re-gasta IA al volver).
        if self.company_dest_id and self.company_dest_id != self.env.company:
            raise UserError(_(
                "Esta factura corresponde a la compañía «%(dest)s», pero estás "
                "trabajando en «%(actual)s».\n\nCambiá a «%(dest)s» (selector de "
                "compañía, arriba a la derecha) y volvé a leer la factura: no se "
                "vuelve a gastar IA, la lectura quedó guardada.",
                dest=self.company_dest_id.name, actual=self.env.company.name))
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

        # Corrección de responsabilidad del proveedor según el comprobante.
        if (self.detected_resp_id
                and self.partner_id.l10n_ar_afip_responsibility_type_id != self.detected_resp_id):
            self.partner_id.l10n_ar_afip_responsibility_type_id = self.detected_resp_id.id

        order = self.env["purchase.order"].create({
            "partner_id": self.partner_id.id,
            "partner_ref": self.partner_ref or False,
            "date_order": fields.Datetime.now(),
            "currency_id": self.currency_id.id or self.partner_id.property_purchase_currency_id.id or False,
        })
        # Crear líneas solo con product_id + cantidad; el price_unit se fuerza
        # después (campo computado-almacenado que Odoo pisa al crear — C.10/C.11).
        has_discount = "discount" in self.env["purchase.order.line"]._fields
        # Percepciones nativas a sumar a cada línea (además del IVA del producto).
        perc_taxes = self.percepcion_ids.mapped("tax_id")
        # IVA a forzar según responsabilidad (No Aplica monotributo / Exento); para
        # Resp. Inscripto queda vacío y se respeta el IVA del producto.
        iva_tax = self._iva_tax_for_responsibility(
            order.company_id, self.partner_id.l10n_ar_afip_responsibility_type_id)
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
            if iva_tax:
                # Monotributo/Exento: reemplazamos el IVA del producto por el 0%
                # que corresponde + las percepciones.
                write_vals["tax_ids"] = [(6, 0, (iva_tax | perc_taxes).ids)]
            elif perc_taxes:
                # (4, id) suma la percepción sin pisar el IVA que trae el producto.
                write_vals["tax_ids"] = [(4, t.id) for t in perc_taxes]
            line.write(write_vals)
            self._seed_supplierinfo(wl)
            self._seed_serials(wl, line)

        # Marca la OC como cargada por IA y guarda nro/fecha de la factura del
        # proveedor para volcarlos al crear la factura (action_ia_create_invoice).
        self.env["po.ia.import.source"].create({
            "order_id": order.id,
            "invoice_number": self.partner_ref or False,
            "invoice_date": self.date_order or False,
        })

        self._attach_source_pdf(order)

        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": order.id,
            "view_mode": "form",
            "target": "current",
        }

    def _attach_source_pdf(self, order):
        """Adjunta el PDF origen (la factura/presupuesto del proveedor del que se
        tomaron los datos) al PO, agrega una comparación de totales Factura vs
        Presupuesto y lo deja todo en el chatter (C.4)."""
        attachment_ids = []
        fname = self.pdf_filename or "presupuesto_proveedor.pdf"
        if self.pdf_file:
            att = self.env["ir.attachment"].create({
                "name": fname,
                "datas": self.pdf_file,
                "res_model": "purchase.order",
                "res_id": order.id,
                "mimetype": "application/pdf",
            })
            attachment_ids = [att.id]

        body = Markup(
            "<p>Presupuesto cargado por IA (Claude Haiku) a partir del archivo "
            "<strong>%s</strong> del proveedor%s.</p>"
        ) % (html_escape(fname), Markup(" (adjunto)") if attachment_ids else Markup(""))
        body += self._totales_comparison_html(order)
        if self.notes and self.notes.strip():
            body += Markup("<p><strong>Notas de la IA:</strong> %s</p>") % html_escape(self.notes.strip())

        order.message_post(
            body=body,
            attachment_ids=attachment_ids,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def _totales_comparison_html(self, order):
        """Tabla HTML comparando los totales del PDF contra los del presupuesto."""
        if not (self.pdf_neto or self.pdf_iva or self.pdf_perc or self.pdf_total):
            return Markup("")
        cur = order.currency_id or self.env.company.currency_id

        def fmt(v):
            return html_escape("{:,.2f}".format(v or 0.0))

        rows = [
            ("Neto gravado", self.pdf_neto, order.amount_untaxed),
            ("Impuestos (IVA + percep.)", (self.pdf_iva or 0.0) + (self.pdf_perc or 0.0), order.amount_tax),
            ("Total", self.pdf_total, order.amount_total),
        ]
        trs = Markup("")
        for label, pdf_v, po_v in rows:
            diff = (po_v or 0.0) - (pdf_v or 0.0)
            ok = abs(diff) <= 1.0
            trs += Markup(
                "<tr><td>%s</td><td style='text-align:right'>%s</td>"
                "<td style='text-align:right'>%s</td>"
                "<td style='text-align:right'>%s %s</td></tr>"
            ) % (html_escape(label), fmt(pdf_v), fmt(po_v), fmt(diff),
                 Markup("✓") if ok else Markup("⚠"))
        return Markup(
            "<p><strong>Comparación de totales (%s)</strong></p>"
            "<table class='table table-sm o_main_table'>"
            "<thead><tr><th>Concepto</th><th style='text-align:right'>Factura (PDF)</th>"
            "<th style='text-align:right'>Presupuesto</th>"
            "<th style='text-align:right'>Δ</th></tr></thead>"
            "<tbody>%s</tbody></table>"
        ) % (html_escape(cur.name or ""), trs)

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

    def _seed_serials(self, wl, po_line):
        """Persiste los seriales de la línea del wizard en `po.ia.line.serial`
        (uno por unidad), para volcarlos a la recepción al confirmar la OC.

        Dedupe por (po_line, serial) — B.7. No bloquea por cantidad: si el nro
        de seriales no coincide con la cantidad, igual se guardan; el aviso se
        emite al confirmar la OC (purchase_order).
        """
        serials = wl._series_list()
        if not serials:
            return
        Serial = self.env["po.ia.line.serial"]
        for s in serials:
            if Serial.search_count([
                ("po_line_id", "=", po_line.id), ("serial", "=", s)]):
                continue
            Serial.create({"po_line_id": po_line.id, "serial": s})

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
    series = fields.Text(
        string="Series",
        help="Números de serie del renglón, UNO por línea (una por unidad). "
             "Al confirmar la OC se vuelcan a la recepción como serie/lote de "
             "los productos trackeados por serie. Editable.")
    series_count = fields.Integer(
        string="# Series", compute="_compute_series_count")

    @api.depends("series")
    def _compute_series_count(self):
        for line in self:
            line.series_count = len(line._series_list())

    def _series_list(self):
        """Lista de seriales saneada (acepta separados por enter o coma)."""
        self.ensure_one()
        raw = (self.series or "").replace(",", "\n")
        return [s.strip() for s in raw.split("\n") if s.strip()]

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


class PoIaImportWizardPerception(models.TransientModel):
    _name = "po.ia.import.wizard.perception"
    _description = "Percepción detectada en el PDF de compra (IA)"

    wizard_id = fields.Many2one("po.ia.import.wizard", required=True, ondelete="cascade")
    tipo = fields.Char(string="Tipo")
    jurisdiccion = fields.Char(string="Jurisdicción")
    alicuota = fields.Float(string="Alícuota %")
    importe = fields.Float(string="Importe (PDF)", readonly=True)
    tax_id = fields.Many2one(
        "account.tax", string="Impuesto (percepción)",
        domain="[('type_tax_use','=','purchase')]",
        help="Impuesto nativo de compra que se agregará a las líneas del presupuesto. "
             "Si quedó vacío, no se encontró uno que coincida: elegilo a mano o dejá la "
             "fila sin impuesto (no se cargará esa percepción).")
