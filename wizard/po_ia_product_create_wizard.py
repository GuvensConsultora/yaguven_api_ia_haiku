import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PoIaProductCreateWizard(models.TransientModel):
    """Sub-wizard lanzado desde una línea sin match del wizard de carga por IA.

    Permite, sin salir del flujo:
      - modo 'variant': sobre una plantilla (medida) existente, fijar un valor por
        atributo (Marca, Modelo, ...) eligiendo uno existente o escribiendo uno nuevo,
        y generar la variante (los atributos del catálogo son `create_variant=dynamic`).
      - modo 'new_tmpl': crear una plantilla nueva (medida que aún no existe) y, sobre
        ella, fijar los valores de atributo y generar la variante.

    Dedupe (B.7): plantilla por nombre, valor de atributo por (atributo, nombre),
    variante por combinación. Si ya existen, se reutilizan.
    """

    _name = "po.ia.product.create.wizard"
    _description = "Alta de producto/variante desde el macheo de líneas (IA Haiku)"

    line_id = fields.Many2one(
        "po.ia.import.wizard.line", string="Línea de origen",
        required=True, ondelete="cascade")

    mode = fields.Selection(
        [("variant", "Variante de una medida existente"),
         ("new_tmpl", "Medida nueva")],
        string="Qué crear", default="variant", required=True)

    # Datos traídos de la línea, para referencia.
    src_codigo = fields.Char(string="Código (PDF)", readonly=True)
    src_descripcion = fields.Char(string="Descripción (PDF)", readonly=True)

    # Modo 'variant': plantilla (medida) existente.
    product_tmpl_id = fields.Many2one(
        "product.template", string="Medida (plantilla)",
        domain="[('attribute_line_ids','!=',False)]")

    # Modo 'new_tmpl': datos de la plantilla nueva.
    new_tmpl_name = fields.Char(string="Nombre de la medida")
    categ_id = fields.Many2one(
        "product.category", string="Categoría",
        default=lambda self: self._default_categ())

    attr_line_ids = fields.One2many(
        "po.ia.product.create.attr", "create_wizard_id", string="Atributos")

    # ------------------------------------------------------------------
    def _default_categ(self):
        categ = self.env.ref("product.product_category_all", raise_if_not_found=False)
        return categ or self.env["product.category"].search([], limit=1)

    @api.onchange("mode", "product_tmpl_id")
    def _onchange_load_attributes(self):
        """Arma una fila por atributo de la plantilla (o por Marca/Modelo para medida nueva)."""
        for wiz in self:
            rows = []
            if wiz.mode == "variant" and wiz.product_tmpl_id:
                for ptal in wiz.product_tmpl_id.attribute_line_ids:
                    rows.append((0, 0, {"attribute_id": ptal.attribute_id.id}))
            elif wiz.mode == "new_tmpl":
                for name in ("Marca", "Modelo"):
                    attr = self.env["product.attribute"].search(
                        [("name", "=", name)], limit=1)
                    if attr:
                        rows.append((0, 0, {"attribute_id": attr.id}))
            wiz.attr_line_ids = [(5, 0, 0)] + rows

    # ------------------------------------------------------------------
    def action_apply(self):
        self.ensure_one()
        # Ignoramos filas totalmente vacías (ej. la fila en blanco de la grilla editable).
        rows = self.attr_line_ids.filtered(
            lambda r: r.attribute_id or r.value_id or (r.new_value or "").strip())
        if not rows:
            raise UserError(_("Cargá al menos un atributo (Marca, Modelo, ...)."))

        tmpl = self._get_or_create_template()

        # Resolver cada atributo a su product.template.attribute.value (ptav),
        # creando el valor y/o sumándolo a la línea de atributo si hace falta.
        combination = self.env["product.template.attribute.value"]
        for row in rows:
            ptav = self._resolve_attribute_value(tmpl, row)
            combination |= ptav

        # Generar (o reutilizar) la variante para esa combinación.
        variant = tmpl._get_variant_for_combination(combination)
        if not variant:
            variant = tmpl._create_product_variant(combination)
        if not variant:
            raise UserError(_("No se pudo generar la variante con esa combinación."))

        # Código propio si vino del PDF y está libre (dedupe B.7).
        codigo = (self.src_codigo or "").strip()
        if codigo and not variant.default_code:
            clash = self.env["product.product"].search_count(
                [("default_code", "=", codigo), ("id", "!=", variant.id)])
            if not clash:
                variant.default_code = codigo

        # Devolver a la línea de origen.
        self.line_id.write({
            "product_id": variant.id,
            "match_status": "created",
        })
        return self._return_to_import_wizard()

    # ------------------------------------------------------------------
    def _get_or_create_template(self):
        if self.mode == "variant":
            if not self.product_tmpl_id:
                raise UserError(_("Elegí la medida (plantilla) sobre la que crear la variante."))
            return self.product_tmpl_id
        # Medida nueva.
        name = (self.new_tmpl_name or "").strip()
        if not name:
            raise UserError(_("Indicá el nombre de la medida nueva."))
        existing = self.env["product.template"].search([("name", "=", name)], limit=1)
        if existing:
            # Dedupe: la medida ya existía; la reutilizamos.
            self.product_tmpl_id = existing
            return existing
        vals = {
            "name": name,
            "categ_id": self.categ_id.id or self._default_categ().id,
            "purchase_ok": True,
            "sale_ok": True,
        }
        if "is_storable" in self.env["product.template"]._fields:
            vals["is_storable"] = True
        return self.env["product.template"].create(vals)

    def _resolve_attribute_value(self, tmpl, row):
        """Devuelve el product.template.attribute.value de `row` sobre `tmpl`,
        creando el valor de atributo y/o agregándolo a la línea de atributo si falta."""
        attr = row.attribute_id
        if not attr:
            raise UserError(_("Hay una fila de atributo sin atributo seleccionado."))
        value = row.value_id
        new_value = (row.new_value or "").strip()
        if not value and not new_value:
            raise UserError(_("Completá el valor del atributo '%s' (elegí uno o escribí uno nuevo).") % attr.name)
        if not value and new_value:
            # Dedupe valor por (atributo, nombre).
            value = self.env["product.attribute.value"].search(
                [("attribute_id", "=", attr.id), ("name", "=ilike", new_value)], limit=1)
            if not value:
                value = self.env["product.attribute.value"].create(
                    {"attribute_id": attr.id, "name": new_value})

        # Asegurar la línea de atributo en la plantilla y que el valor esté incluido.
        ptal = self.env["product.template.attribute.line"].search(
            [("product_tmpl_id", "=", tmpl.id), ("attribute_id", "=", attr.id)], limit=1)
        if not ptal:
            ptal = self.env["product.template.attribute.line"].create({
                "product_tmpl_id": tmpl.id,
                "attribute_id": attr.id,
                "value_ids": [(6, 0, [value.id])],
            })
        elif value not in ptal.value_ids:
            ptal.write({"value_ids": [(4, value.id)]})

        ptav = self.env["product.template.attribute.value"].search([
            ("product_tmpl_id", "=", tmpl.id),
            ("attribute_id", "=", attr.id),
            ("product_attribute_value_id", "=", value.id),
        ], limit=1)
        if not ptav:
            raise UserError(_(
                "No se pudo vincular el valor '%(val)s' del atributo '%(attr)s' a la medida.",
                val=value.name, attr=attr.name))
        return ptav

    def _return_to_import_wizard(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "po.ia.import.wizard",
            "res_id": self.line_id.wizard_id.id,
            "view_mode": "form",
            "target": "new",
        }


class PoIaProductCreateAttr(models.TransientModel):
    _name = "po.ia.product.create.attr"
    _description = "Fila de atributo para alta de variante (IA Haiku)"

    create_wizard_id = fields.Many2one(
        "po.ia.product.create.wizard", required=True, ondelete="cascade")
    attribute_id = fields.Many2one("product.attribute", string="Atributo")
    value_id = fields.Many2one(
        "product.attribute.value", string="Valor existente",
        domain="[('attribute_id','=',attribute_id)]")
    new_value = fields.Char(
        string="Valor nuevo",
        help="Escribí acá un valor que todavía no exista (ej. un Modelo nuevo). "
             "Se da de alta en el atributo y se agrega a la medida.")
