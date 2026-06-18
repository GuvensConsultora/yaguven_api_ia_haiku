import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PoIaProductCreateWizard(models.TransientModel):
    """Sub-wizard lanzado desde una línea sin match del wizard de carga por IA.

    Flujo mínimo, sin salir de la carga: elegís la MEDIDA (plantilla; existente o
    una nueva tipeándola en el m2o), elegís los ATRIBUTOS y sus VALORES en el momento
    (Marca, Modelo, ... cada uno con un valor existente o uno nuevo) y la REFERENCIA.
    Se genera la variante (los atributos del catálogo son `create_variant=dynamic`).

    Dedupe (B.7): valor de atributo por (atributo, nombre) y variante por combinación;
    si ya existen, se reutilizan.
    """

    _name = "po.ia.product.create.wizard"
    _description = "Alta de producto/variante desde el macheo de líneas (IA Haiku)"

    line_id = fields.Many2one(
        "po.ia.import.wizard.line", string="Línea de origen",
        required=True, ondelete="cascade")

    # Datos traídos de la línea, para referencia.
    src_codigo = fields.Char(string="Código (PDF)", readonly=True)
    src_descripcion = fields.Char(string="Descripción (PDF)", readonly=True)

    ref_code = fields.Char(
        string="Referencia (código interno)",
        default=lambda self: self.env.context.get("default_src_codigo") or False,
        help="Código interno (default_code) que tendrá el producto/variante creado. "
             "Se precarga con el código del PDF; podés editarlo o dejarlo en blanco.")

    # Medida = plantilla. Elegís una existente o tipeás una nueva (se crea sola).
    product_tmpl_id = fields.Many2one(
        "product.template", string="Medida")

    attr_line_ids = fields.One2many(
        "po.ia.product.create.attr", "create_wizard_id", string="Atributos")

    # ------------------------------------------------------------------
    @api.onchange("product_tmpl_id")
    def _onchange_load_attributes(self):
        """Si la medida ya tiene atributos, precarga una fila por cada uno (solo a
        completar el valor). Si es una medida nueva, deja la grilla vacía para que
        elijas los atributos y valores en el momento."""
        for wiz in self:
            rows = []
            if wiz.product_tmpl_id:
                for ptal in wiz.product_tmpl_id.attribute_line_ids:
                    rows.append((0, 0, {"attribute_id": ptal.attribute_id.id}))
            wiz.attr_line_ids = [(5, 0, 0)] + rows

    # ------------------------------------------------------------------
    def action_apply(self):
        self.ensure_one()
        # Ignoramos filas totalmente vacías (ej. la fila en blanco de la grilla editable).
        rows = self.attr_line_ids.filtered(
            lambda r: r.attribute_id or r.value_id or (r.new_value or "").strip())
        if not rows:
            raise UserError(_("Cargá al menos un atributo (Marca, Modelo, ...)."))
        if not self.product_tmpl_id:
            raise UserError(_("Elegí una medida (o tipeá una nueva para crearla)."))

        tmpl = self.product_tmpl_id
        # En un flujo de compras la medida tiene que poder comprarse y stockearse.
        tmpl_vals = {}
        if not tmpl.purchase_ok:
            tmpl_vals["purchase_ok"] = True
        if "is_storable" in tmpl._fields and not tmpl.is_storable:
            tmpl_vals["is_storable"] = True
        if tmpl_vals:
            tmpl.write(tmpl_vals)

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

        # Referencia (default_code) del producto/variante. Editable; dedupe B.7.
        ref = (self.ref_code or "").strip()
        if ref and ref != variant.default_code:
            clash = self.env["product.product"].search_count(
                [("default_code", "=", ref), ("id", "!=", variant.id)])
            if clash:
                raise UserError(_(
                    "Ya existe otro producto con la referencia '%s'. Usá otra o dejala en blanco.") % ref)
            variant.default_code = ref

        # Devolver a la línea de origen.
        self.line_id.write({
            "product_id": variant.id,
            "match_status": "created",
        })
        return self._return_to_import_wizard()

    # ------------------------------------------------------------------
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
