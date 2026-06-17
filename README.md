# Yagüven — Carga de presupuestos de compra con IA (Haiku)

Lee un **PDF de presupuesto / oferta / lista de precios** de un proveedor con
**Claude Haiku** y arma un **presupuesto de compra (`purchase.order`) en borrador**,
matcheando cada línea contra el catálogo y sembrando la base de equivalencias del
proveedor (`product.supplierinfo`).

## Para qué sirve (funcional)

El proveedor manda su presupuesto en PDF, con **su** código y **su** descripción de
cada producto. En vez de cargar el RFQ a mano renglón por renglón:

1. Subís el PDF.
2. La IA extrae proveedor, referencia, fecha, moneda y las líneas
   (código del proveedor, descripción, cantidad, precio, descuento).
3. Odoo propone el producto de tu catálogo para cada línea.
4. Revisás/corregís y confirmás.
5. Se crea el presupuesto de compra **en borrador** (nunca confirmado): lo revisás
   y lo confirmás vos.

### Cómo aprende (cold start)

Hoy la base `product.supplierinfo` casi no tiene cargado el código con que cada
proveedor llama a los productos, así que las **primeras** cargas de un proveedor
matchean poco y las completás a mano. **Cada línea que confirmás siembra la
`supplierinfo`** (código + nombre + precio del proveedor). A partir de la 2ª/3ª
factura de ese proveedor, esas líneas matchean **solas**.

## Orden del match

Por cada línea del PDF, en este orden:

1. **`product.supplierinfo` del proveedor** por su `product_code` → *Auto (proveedor)*.
2. **`default_code` propio** (por si el proveedor usa tu código) → *Auto (código)*.
3. **Nombre `ilike`** con match único → *Sugerido*.
4. Si nada matchea → *Sin match* (lo completás en el wizard).

El estado de cada línea se ve en una **etiqueta de color** (verde / azul / rojo).

## Uso

1. **Ajustes → Compras → IA Haiku**: cargá la *API Key (Claude Haiku)*.
   (Si se deja vacío, usa el parámetro `camiletti.anthropic_api_key`.)
2. **Compras → Operaciones → Cargar presupuesto con IA**.
3. Subí el PDF → **Leer con IA**.
4. Revisá proveedor y líneas → **Crear presupuesto**.

## Diseño técnico

- **Conector** `ia.haiku.service` (`AbstractModel`, reusable): llama a la API REST de
  Anthropic con `requests`. El PDF va como bloque `document` nativo (sin OCR previo).
  Modelo: `claude-haiku-4-5-20251001`.
- **No invasivo (C.2):** no se agregan campos a modelos nativos; el único `_inherit`
  es `res.config.settings` (patrón estándar de Ajustes) y la siembra usa la
  `product.supplierinfo` nativa.
- **`price_unit`** se escribe **después** de crear la línea (campo computado-almacenado
  que Odoo recalcula al crear con `product_id` — gotcha C.10/C.11).
- **`discount`** se completa solo si el campo existe en la instancia (sin depender de
  OCA).
- **Dedupe (B.7):** no duplica `supplierinfo` `(partner, product_tmpl, product_code)`;
  avisa si ya existe un RFQ draft del mismo proveedor + referencia.
- **Siempre draft (B.5):** nunca confirma el presupuesto.

### Parámetros

| Parámetro (`ir.config_parameter`) | Uso |
|---|---|
| `api_ia_haiku.api_key` | API Key de Anthropic (principal). |
| `camiletti.anthropic_api_key` | Respaldo si el principal está vacío. |

## Ejercicios de verificación (A.4)

- **Credencial activa:** Ajustes → Técnico → Parámetros del sistema → buscar
  `api_ia_haiku.api_key`. Debe tener tu key.
- **Match por proveedor funciona:** cargá un PDF de un proveedor, confirmá una línea
  que quedó *Sin match* eligiendo el producto. Andá a ese producto →
  pestaña *Compra* → *Proveedores*: debe aparecer la línea con el **código del
  proveedor** y el **precio** que cargaste. Volvé a subir el mismo PDF: esa línea
  ahora debe salir *Auto (proveedor)*.
- **Precio respetado:** abrí el presupuesto creado y compará el `price_unit` de cada
  línea contra el PDF — deben coincidir (no el precio de lista del producto).
- **No se confirma solo:** el presupuesto queda en estado **Solicitud de
  presupuesto** (borrador), nunca confirmado.

## Alcance / límites (por etapas)

Este ladrillo arma la cabecera + líneas con producto, precio y descuento. **No**
incluye (ladrillos siguientes): refinar el match dudoso con una 2ª llamada a la IA
pasándole los candidatos del catálogo, ni la conciliación con la factura posterior.

---
Yagüven C.G.
