from odoo import api, fields, models, _


class PoIaImportSource(models.Model):
    """Marca una purchase.order como creada por el wizard de IA y guarda los datos
    de la factura del proveedor (nro y fecha) para volcarlos al crear la factura.

    Vive en tabla propia con m2o al nativo (C.2): no agrega columnas a purchase.order.
    """

    _name = "po.ia.import.source"
    _description = "Origen IA de una OC (factura del proveedor)"

    order_id = fields.Many2one(
        "purchase.order", string="Orden de compra",
        required=True, ondelete="cascade", index=True)
    invoice_number = fields.Char(string="Nro de factura del proveedor")
    invoice_date = fields.Date(string="Fecha de factura")


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    # Computado no almacenado (sin columna): solo para la lógica de la vista.
    ia_haiku_origin = fields.Boolean(
        string="Cargada por IA (Haiku)", compute="_compute_ia_haiku_origin")

    def _compute_ia_haiku_origin(self):
        srcs = self.env["po.ia.import.source"].search([("order_id", "in", self.ids)])
        ids_with_src = set(srcs.mapped("order_id").ids)
        for po in self:
            po.ia_haiku_origin = po.id in ids_with_src

    def action_ia_create_invoice(self):
        """Crea la factura de proveedor para una OC cargada por IA, con las
        CANTIDADES COMPLETAS de la orden (no las recibidas) y la fecha/nro de la
        factura del proveedor. Para OC no-IA, cae al flujo nativo."""
        self.ensure_one()
        source = self.env["po.ia.import.source"].search(
            [("order_id", "=", self.id)], limit=1)
        if not source:
            return self.action_create_invoice()

        move_vals = self._prepare_invoice()
        if source.invoice_date:
            move_vals["invoice_date"] = source.invoice_date
        if source.invoice_number:
            move_vals["ref"] = source.invoice_number

        line_cmds = []
        for line in self.order_line:
            mv = line._prepare_account_move_line()
            if not line.display_type:
                # Cantidad completa de la OC (la factura ya existe, no esperamos recepción).
                mv["quantity"] = line.product_qty
            line_cmds.append((0, 0, mv))
        move_vals["invoice_line_ids"] = line_cmds

        move = self.env["account.move"].with_company(self.company_id).create(move_vals)
        return {
            "type": "ir.actions.act_window",
            "name": _("Factura de proveedor"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "target": "current",
        }
