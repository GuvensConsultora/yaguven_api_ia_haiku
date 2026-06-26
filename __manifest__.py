{
    "name": "Yagüven — Carga de presupuestos de compra con IA (Haiku)",
    "version": "19.0.2.7.0",
    "author": "Yagüven C.G.",
    "website": "https://yaguven.com",
    "category": "Purchases",
    "summary": "Lee un PDF de oferta/lista del proveedor con Claude Haiku y arma un "
               "presupuesto de compra (RFQ) en borrador, matcheando líneas al catálogo "
               "y sembrando product.supplierinfo (código y precio del proveedor).",
    "description": """
Carga de presupuestos de compra con IA (Haiku)
==============================================

Flujo
-----
1. Se sube el PDF de la oferta/lista de precios del proveedor.
2. Claude Haiku extrae los datos estructurados (proveedor, referencia, fecha,
   moneda y líneas con código del proveedor, descripción, cantidad y precio).
3. Odoo matchea cada línea contra el catálogo:
   - 1º por ``product.supplierinfo`` del proveedor (``product_code``),
   - 2º por ``default_code`` propio,
   - 3º por nombre (``ilike``) — asistido en el wizard.
4. El usuario revisa/corrige el match y confirma.
5. Se crea el ``purchase.order`` en **borrador** (nunca confirmado).
6. Al confirmar cada línea se crea/actualiza la ``product.supplierinfo``
   (código, nombre y precio del proveedor) para que la próxima carga
   de ese proveedor matchee automáticamente.

Diseño
------
- No invasivo (C.2): el conector a la API vive en un modelo propio
  (``ia.haiku.service``); no se hereda de modelos nativos salvo para el botón.
- La credencial se guarda en ``ir.config_parameter`` (``api_ia_haiku.api_key``),
  con *fallback* a ``camiletti.anthropic_api_key``.
- Respeta los gotchas de carga de ``purchase.order`` por ORM: ``price_unit`` se
  escribe directo después del create (campo computado-almacenado).
""",
    "depends": [
        "purchase_stock",
        "l10n_ar",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/purchase_order_views.xml",
        "views/stock_picking_views.xml",
        "wizard/po_ia_import_wizard_views.xml",
        "wizard/po_ia_product_create_wizard_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
