from odoo import api, fields, models


class PoIaLineSerial(models.Model):
    """Serial capturado en el presupuesto (línea de OC) para volcarlo a la
    recepción.

    Vive en tabla propia con m2o a `purchase.order.line` (C.2): no agrega
    columnas al nativo, upgrade-safe y desinstalación limpia. Una fila = una
    unidad serializada. Al confirmar la OC, estos seriales se vuelcan a las
    `stock.move.line` de la recepción (`lot_name`) para los productos
    `tracking='serial'`.
    """

    _name = "po.ia.line.serial"
    _description = "Serial de línea de OC para volcar a la recepción"
    _order = "po_line_id, serial"

    po_line_id = fields.Many2one(
        "purchase.order.line", string="Línea de OC",
        required=True, ondelete="cascade", index=True)
    order_id = fields.Many2one(
        related="po_line_id.order_id", string="Orden de compra",
        store=True, index=True)
    product_id = fields.Many2one(
        related="po_line_id.product_id", string="Producto", store=True)
    company_id = fields.Many2one(
        related="po_line_id.company_id", string="Compañía", store=True)
    serial = fields.Char(string="Serie", required=True)
    # Estado de volcado: una vez que el serial llegó a una move.line de la
    # recepción, se marca para no volver a crearlo (idempotencia, B.7).
    transferred = fields.Boolean(string="Volcado a recepción", default=False)

    _sql_constraints = [
        ("uniq_serial_per_line", "unique(po_line_id, serial)",
         "No se puede repetir el mismo serial en una línea de OC."),
    ]
