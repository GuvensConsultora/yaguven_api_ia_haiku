from markupsafe import Markup

from odoo import _, models
from odoo.tools.misc import html_escape


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def _yaguven_load_po_serials(self):
        """Pre-carga BLANDA: vuelca a las move lines de la recepción los
        seriales capturados en el ppto por IA (`po.ia.line.serial`), como
        serie/lote editable. NO fija nada: el operario confirma/corrige contra
        lo físico al validar (C.12 — la recepción sigue siendo la verdad).

        - Solo productos `tracking='serial'` y pickings con `use_create_lots`.
        - Una move line por unidad (`quantity=1`, `lot_name=serial`).
        - Idempotente (B.7): si el move ya tiene líneas con serie cargada, no
          vuelve a tocar (respeta la edición previa del operario).
        - Aviso (no bloqueo) por chatter si el nº de series ≠ cantidad del move.
        """
        Serial = self.env["po.ia.line.serial"]
        MoveLine = self.env["stock.move.line"]
        for picking in self:
            if picking.state in ("done", "cancel"):
                continue
            if not picking.picking_type_id.use_create_lots:
                continue
            mismatches = []
            for move in picking.move_ids:
                if move.product_id.tracking != "serial":
                    continue
                po_line = move.purchase_line_id
                if not po_line:
                    continue
                serials = Serial.search([
                    ("po_line_id", "=", po_line.id),
                    ("transferred", "=", False),
                ])
                if not serials:
                    continue
                # Idempotencia: si ya hay líneas con serie, no re-cargar.
                if move.move_line_ids.filtered(lambda l: l.lot_name or l.lot_id):
                    continue
                names = serials.mapped("serial")
                if len(names) != move.product_uom_qty:
                    mismatches.append(
                        (move.product_id.display_name, len(names),
                         move.product_uom_qty))
                # Reutiliza las move lines vacías que arma Odoo; crea el resto.
                empties = list(move.move_line_ids.filtered(
                    lambda l: not l.lot_name and not l.lot_id))
                for name in names:
                    if empties:
                        empties.pop(0).write({"lot_name": name, "quantity": 1.0})
                    else:
                        MoveLine.create({
                            "move_id": move.id,
                            "picking_id": picking.id,
                            "product_id": move.product_id.id,
                            "product_uom_id": move.product_uom.id,
                            "location_id": move.location_id.id,
                            "location_dest_id": move.location_dest_id.id,
                            "quantity": 1.0,
                            "lot_name": name,
                        })
                serials.write({"transferred": True})
            if mismatches:
                rows = "".join(
                    "<li>%s: %s series en la OC vs %s de cantidad</li>" % (
                        html_escape(prod), html_escape(str(ns)),
                        html_escape(str(qty)))
                    for prod, ns, qty in mismatches)
                body = Markup(
                    "<p><strong>Series pre-cargadas desde la OC (IA).</strong> "
                    "Revisá contra lo físico: el nº de series no coincide con la "
                    "cantidad en:</p><ul>" + rows + "</ul>")
                picking.message_post(
                    body=body, message_type="comment",
                    subtype_xmlid="mail.mt_note")

    def action_yaguven_load_po_serials(self):
        """Botón en la recepción: re-sincroniza las series de la OC."""
        self._yaguven_load_po_serials()
        return True
