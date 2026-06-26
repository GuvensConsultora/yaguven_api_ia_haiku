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

    def button_confirm(self):
        """Tras confirmar (que genera la recepción), vuelca a las move lines
        de la recepción los seriales capturados en el ppto por IA.

        El volcado vive en `stock.picking._yaguven_load_po_serials` (C.2:
        herencia, lógica en su modelo). Es idempotente: si ya se cargaron, no
        duplica (B.7)."""
        res = super().button_confirm()
        self.picking_ids._yaguven_load_po_serials()
        return res

    def action_ia_create_invoice(self):
        """Crea la factura de proveedor para cualquier OC con las CANTIDADES
        COMPLETAS de la orden (no las recibidas). Si la OC fue cargada por IA,
        además vuelca la fecha y el nro de la factura del proveedor."""
        self.ensure_one()
        source = self.env["po.ia.import.source"].search(
            [("order_id", "=", self.id)], limit=1)

        move_vals = self._prepare_invoice()
        # Nro de factura: lo tomamos de la OC (Referencia del proveedor); si no,
        # del dato que cargó el wizard de IA.
        ref = self.partner_ref or source.invoice_number
        if ref:
            move_vals["ref"] = ref
        if source.invoice_date:
            move_vals["invoice_date"] = source.invoice_date

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


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    # Relación inversa a la tabla propia de seriales (C.2: no agrega columna de
    # dato al nativo; el o2m es virtual). Los seriales se siembran al crear la
    # OC desde el wizard de IA y se vuelcan a la recepción al confirmar.
    ia_serial_ids = fields.One2many(
        "po.ia.line.serial", "po_line_id", string="Series (IA)")
    ia_serials_display = fields.Char(
        string="Nro Serie", compute="_compute_ia_serials_display",
        inverse="_inverse_ia_serials_display", store=False,
        help="Series leídas de la factura del proveedor (una por unidad). "
             "Editable: si las corregís acá, se actualizan las que se vuelcan "
             "a la recepción al confirmar la OC.")

    @api.depends("ia_serial_ids.serial")
    def _compute_ia_serials_display(self):
        for line in self:
            line.ia_serials_display = ", ".join(line.ia_serial_ids.mapped("serial"))

    def _inverse_ia_serials_display(self):
        Serial = self.env["po.ia.line.serial"]
        for line in self:
            raw = (line.ia_serials_display or "").replace("\n", ",")
            serials = [s.strip() for s in raw.split(",") if s.strip()]
            # Recrea la lista (dedupe por orden de tipeo). No reescribe si no
            # cambió, para no resembrar al solo abrir/guardar.
            if serials == line.ia_serial_ids.mapped("serial"):
                continue
            line.ia_serial_ids.unlink()
            for s in serials:
                Serial.create({"po_line_id": line.id, "serial": s})
