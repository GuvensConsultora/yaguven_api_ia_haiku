from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    api_ia_haiku_api_key = fields.Char(
        string="API Key (Claude Haiku)",
        config_parameter="api_ia_haiku.api_key",
        help="Credencial de la API de Anthropic usada para leer presupuestos de "
             "compra (PDF) con IA. Si se deja vacío, el módulo usa el parámetro "
             "'camiletti.anthropic_api_key' como respaldo.",
    )
