from odoo import fields, models


class PoIaExtractionCache(models.Model):
    """Cachea el resultado de la lectura de un PDF por la IA, indexado por el hash
    del archivo. Evita re-gastar Haiku si se vuelve a leer la misma factura
    (p. ej. tras cambiar de compañía y reabrir el wizard)."""

    _name = "po.ia.extraction.cache"
    _description = "Caché de extracción IA de PDF de compra"

    pdf_hash = fields.Char(string="Hash del PDF", required=True, index=True)
    result_json = fields.Text(string="Resultado (JSON)")
