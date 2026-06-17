import base64
import json
import logging

import requests

from odoo import api, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Haiku 4.5 — lee PDF nativo (bloque document), sin OCR previo.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Esquema que le pedimos devolver a Haiku. El código del proveedor (no el nuestro)
# es la clave para sembrar product.supplierinfo en la confirmación.
EXTRACTION_SYSTEM = (
    "Sos un asistente que extrae datos de presupuestos, ofertas o listas de precios "
    "de proveedores argentinos. Te paso un PDF y devolvés ÚNICAMENTE un objeto JSON "
    "válido, sin texto antes ni después, sin ```. Esquema exacto:\n"
    "{\n"
    '  "proveedor": {"cuit": "<solo dígitos o vacío>", "razon_social": "<str>"},\n'
    '  "referencia": "<nro de presupuesto/oferta del proveedor o vacío>",\n'
    '  "fecha": "<YYYY-MM-DD o vacío>",\n'
    '  "moneda": "<ARS|USD>",\n'
    '  "lineas": [\n'
    '    {"codigo": "<código del proveedor>", "descripcion": "<str>",\n'
    '     "cantidad": <número>, "precio_unit": <número>, "descuento": <número 0-100>}\n'
    "  ],\n"
    '  "confianza": <0.0-1.0>,\n'
    '  "notas": "<dudas o renglones ilegibles>"\n'
    "}\n"
    "Reglas: 'codigo' es el código tal cual lo escribe el proveedor (NO inventes). "
    "Si un dato no está, dejalo vacío o 0. 'precio_unit' sin IVA si se distingue; "
    "si no, el que figure. 'confianza' baja si el PDF está borroso o ambiguo."
)


class IaHaikuService(models.AbstractModel):
    """Conector REST reusable a la API de Claude Haiku.

    No hereda de modelos nativos (C.2). Otros módulos pueden llamar a
    ``self.env['ia.haiku.service'].extract_purchase_quote(pdf_b64)``.
    """

    _name = "ia.haiku.service"
    _description = "Conector IA Haiku (Anthropic)"

    @api.model
    def _get_api_key(self):
        icp = self.env["ir.config_parameter"].sudo()
        key = (icp.get_param("api_ia_haiku.api_key") or "").strip()
        if not key:
            # Fallback a la credencial ya usada por camiletti_inventory_count.
            key = (icp.get_param("camiletti.anthropic_api_key") or "").strip()
        if not key:
            raise UserError(_(
                "No hay API key de Anthropic configurada. Cargala en "
                "Ajustes → Compras → IA Haiku (parámetro 'api_ia_haiku.api_key')."
            ))
        return key

    @api.model
    def _call(self, content, system=None, max_tokens=4096, timeout=120):
        """Llamada cruda a la API. ``content`` = lista de bloques del mensaje user.

        Devuelve dict: {'text': <str respuesta>, 'usage': {...}, 'raw': <json>}.
        """
        api_key = self._get_api_key()
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": HAIKU_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system

        try:
            resp = requests.post(
                ANTHROPIC_API_URL, headers=headers, json=payload, timeout=timeout
            )
        except requests.exceptions.RequestException as e:
            _logger.exception("IA Haiku: error de red")
            raise UserError(_("Error de conexión con la API de IA: %s") % e)

        if resp.status_code != 200:
            _logger.error("IA Haiku: HTTP %s — %s", resp.status_code, resp.text[:500])
            raise UserError(_(
                "La API de IA respondió con error %(code)s:\n%(body)s",
                code=resp.status_code, body=resp.text[:500],
            ))

        try:
            data = resp.json()
            text = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            )
        except (ValueError, KeyError, TypeError) as e:
            _logger.exception("IA Haiku: respuesta no parseable")
            raise UserError(_("No se pudo leer la respuesta de la IA: %s") % e)

        return {"text": text, "usage": data.get("usage", {}), "raw": data}

    @api.model
    def _extract_json(self, text):
        """Parsea el primer objeto JSON del texto (defensivo ante envoltura)."""
        s = (text or "").strip()
        if s.startswith("```"):
            s = s.strip("`")
            s = s[s.find("{"):] if "{" in s else s
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j < i:
            raise UserError(_("La IA no devolvió un JSON reconocible:\n%s") % text[:500])
        try:
            return json.loads(s[i:j + 1])
        except json.JSONDecodeError as e:
            raise UserError(_("JSON inválido de la IA (%s):\n%s") % (e, text[:500]))

    @api.model
    def extract_purchase_quote(self, pdf_b64, filename="presupuesto.pdf"):
        """Lee un PDF de presupuesto de compra y devuelve el dict estructurado.

        :param pdf_b64: PDF en base64 (str).
        :return: dict con claves proveedor/referencia/fecha/moneda/lineas/confianza/notas
                 + '_usage' (tokens) para reporte de consumo.
        """
        if isinstance(pdf_b64, bytes):
            pdf_b64 = pdf_b64.decode()
        # Validar que sea base64 razonable (evita mandar basura a la API).
        try:
            base64.b64decode(pdf_b64, validate=True)
        except Exception:
            raise UserError(_("El archivo no parece un PDF válido en base64."))

        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            },
            {
                "type": "text",
                "text": "Extraé los datos de este presupuesto de compra según el esquema.",
            },
        ]
        res = self._call(content, system=EXTRACTION_SYSTEM, max_tokens=4096)
        parsed = self._extract_json(res["text"])
        parsed["_usage"] = res.get("usage", {})
        _logger.info(
            "IA Haiku: extraído '%s' — %s líneas, usage=%s",
            filename, len(parsed.get("lineas", [])), parsed.get("_usage"),
        )
        return parsed
