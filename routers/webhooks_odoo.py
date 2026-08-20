"""Webhooks disparados desde Odoo — notificaciones de WhatsApp al cliente
cuando se confirma una orden o un trabajo queda listo para retirar."""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

import odoo_crm
import whatsapp
from auth import verificar_api_key
from config import (BRIDGE_API_KEY, WA_MSG_ORDEN_CONFIRMADA, WA_MSG_TRABAJO_LISTO,
                    WA_PLANTILLA_TRABAJO_LISTO_OFICINA, WA_PLANTILLA_TRABAJO_LISTO_TALLER)
from db import (actualizar_ultima_orden, buscar_cliente_odoo_por_id,
                guardar_mensaje, obtener_canonical_id)

log = logging.getLogger(__name__)
router = APIRouter()


def _formatear_monto(valor) -> str:
    try:
        return f"${float(valor):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return ""


async def _extraer_cliente(payload: dict) -> tuple[str, str]:
    """Extrae (telefono, nombre) del payload de Odoo. Busca en sync local si no viene el teléfono.

    partner_id puede venir como dict ({"id", "display_name", ...}), como [id, "Nombre"]
    (formato estándar de Odoo para campos many2one) o como escalar (solo el id)."""
    partner_raw = payload.get("partner_id")

    partner_id = None
    nombre = ""
    if isinstance(partner_raw, dict):
        partner_id = partner_raw.get("id")
        nombre = partner_raw.get("display_name") or ""
    elif isinstance(partner_raw, (list, tuple)) and partner_raw:
        partner_id = partner_raw[0]
        nombre = partner_raw[1] if len(partner_raw) > 1 else ""
    elif isinstance(partner_raw, (int, str)):
        partner_id = partner_raw

    telefono = (
        payload.get("partner_phone")
        or payload.get("partner_mobile")
        or payload.get("telefono")
        or (partner_raw.get("phone")   if isinstance(partner_raw, dict) else None)
        or (partner_raw.get("mobile")  if isinstance(partner_raw, dict) else None)
        or ""
    )
    telefono = "".join(c for c in telefono if c.isdigit())

    if not telefono and partner_id:
        cliente = await buscar_cliente_odoo_por_id(int(partner_id))
        if cliente:
            telefono = "".join(c for c in (cliente.get("telefono") or "") if c.isdigit())
            nombre   = nombre or cliente.get("nombre") or ""

    # Último recurso: la sync local puede estar desactualizada o no tener el
    # partner — buscamos el teléfono directo en Odoo por RPC.
    if not telefono and partner_id:
        telefono = await odoo_crm.obtener_telefono_partner(int(partner_id))

    return telefono, nombre


def _resolver_order_id(payload: dict) -> int | None:
    """Resuelve el id real de la sale.order para dejar la nota en el registro correcto.

    Si el payload es directamente una sale.order, es su "id". Si es una
    project.task (caso de "trabajo listo"), se toma de sale_order_id — que
    puede venir como dict, [id, "nombre"] o ausente si el selector de campos
    del Webhook nativo de Odoo no lo incluyó."""
    if payload.get("_model") == "sale.order" and payload.get("id"):
        return int(payload["id"])
    sale_order = payload.get("sale_order_id")
    if isinstance(sale_order, dict) and sale_order.get("id"):
        return int(sale_order["id"])
    if isinstance(sale_order, (list, tuple)) and sale_order:
        return int(sale_order[0])
    return None


async def _enviar_notificacion_wa(telefono: str, mensaje: str, nro_orden: str,
                                   odoo_model: str = "", odoo_id: int = 0):
    async with httpx.AsyncClient() as client:
        ok = await whatsapp.enviar_mensaje(client, telefono, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje por WhatsApp")
    canonical = await obtener_canonical_id(telefono)
    await guardar_mensaje(canonical, "assistant", f"[Odoo] {mensaje}")
    log.info("Odoo → WA enviado a %s | orden: %s", telefono, nro_orden)

    if odoo_model and odoo_id:
        await odoo_crm.registrar_nota(odoo_model, odoo_id, f"WhatsApp enviado al cliente:\n{mensaje}")


async def _notificar_orden(
    telefono: str, mensaje: str, order_id: int | None, nota_exito: str,
    plantilla: tuple[str, str, list[str]] | None = None,
) -> bool:
    """Envía el WA y deja constancia en el chatter de la orden (éxito o motivo del fallo).

    Si se pasa `plantilla` (nombre, idioma, parametros) se envía como mensaje de
    plantilla de Meta en vez de texto libre — necesario fuera de la ventana de 24hs.

    Nunca lanza — el webhook de Odoo siempre debe recibir 200, de lo contrario
    Odoo reintenta el envío y se duplican los mensajes al cliente."""
    if not telefono:
        log.warning("Sin teléfono registrado — no se envía WhatsApp (orden id=%s)", order_id)
        if order_id:
            await odoo_crm.registrar_nota_orden(
                order_id,
                "⚠️ No se pudo enviar WhatsApp: el cliente no tiene teléfono registrado en Odoo.",
            )
        return False

    enviado = False
    try:
        async with httpx.AsyncClient() as client:
            if plantilla:
                nombre_plantilla, idioma, parametros = plantilla
                enviado = await whatsapp.enviar_plantilla(client, telefono, nombre_plantilla, idioma, parametros)
            else:
                enviado = await whatsapp.enviar_mensaje(client, telefono, mensaje)
    except Exception as e:
        log.error("Error enviando WhatsApp a %s: %s", telefono, e)

    if enviado:
        canonical = await obtener_canonical_id(telefono)
        await guardar_mensaje(canonical, "assistant", f"[Odoo] {mensaje}")
        log.info("Odoo → WA enviado a %s", telefono)
        if order_id:
            await odoo_crm.registrar_nota_orden(order_id, nota_exito)
            # Deja constancia de la orden activa del cliente para que sus mensajes
            # posteriores (ej. comprobante de pago) también se registren en el
            # chatter de esta orden, no solo en el del contacto.
            await actualizar_ultima_orden(canonical, order_id)
    else:
        log.error("No se pudo enviar WhatsApp a %s", telefono)
        if order_id:
            await odoo_crm.registrar_nota_orden(
                order_id,
                "⚠️ No se pudo enviar WhatsApp: error al enviar el mensaje por WhatsApp.",
            )

    return enviado


@router.post("/odoo/webhook")
async def webhook_odoo(request: Request):
    """Endpoint genérico — mantiene compatibilidad con la configuración anterior."""
    await verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook (genérico) payload: %s", payload)
    nro_orden = payload.get("name") or payload.get("display_name") or "—"
    telefono, nombre = await _extraer_cliente(payload)
    if not telefono:
        return {"ok": False, "detalle": f"Sin teléfono para orden {nro_orden}"}
    nombre_corto = nombre.split()[0] if nombre else "te"
    mensaje = payload.get("mensaje") or WA_MSG_TRABAJO_LISTO.format(nombre=nombre_corto, nro_orden=nro_orden)
    await _enviar_notificacion_wa(
        telefono, mensaje, nro_orden,
        odoo_model=str(payload.get("_model") or ""),
        odoo_id=int(payload["id"]) if payload.get("id") else 0,
    )
    return {"ok": True, "telefono": telefono, "orden": nro_orden}


@router.get("/whatsapp/plantillas")
async def whatsapp_plantillas(request: Request):
    """Debug: lista las plantillas de WhatsApp aprobadas en Meta para esta cuenta."""
    await verificar_api_key(request)
    async with httpx.AsyncClient() as client:
        return await whatsapp.obtener_plantillas(client)


@router.post("/odoo/webhook/orden-confirmada")
async def webhook_orden_confirmada(request: Request):
    """Dispara cuando se confirma una orden de venta (sale.order state=sale)."""
    await verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook orden-confirmada payload: %s", payload)

    order_id = _resolver_order_id(payload) or (int(payload["id"]) if payload.get("id") else None)
    nro_orden = payload.get("name") or payload.get("nro_orden") or "—"
    monto = payload.get("amount_total")
    access_url = payload.get("access_url") or ""

    # El selector de campos del Webhook nativo de Odoo es limitado: si no
    # vino el monto, el nombre, el link o el cliente pero sí el id, lo completamos por RPC.
    if (monto is None or not nro_orden or nro_orden == "—" or not access_url or not payload.get("partner_id")) and order_id:
        orden = await odoo_crm.buscar_orden_por_id(order_id)
        if orden:
            nro_orden = nro_orden if nro_orden and nro_orden != "—" else orden.get("name") or "—"
            monto = monto if monto is not None else orden.get("amount_total")
            access_url = access_url or orden.get("access_url") or ""
            if not payload.get("partner_id") and orden.get("partner_id"):
                payload["partner_id"] = orden["partner_id"]

    from config import NOMBRE_NEGOCIO
    monto_fmt = _formatear_monto(monto)
    moneda_simbolo, monto_numero = (monto_fmt[0], monto_fmt[1:]) if monto_fmt else ("$", "—")
    telefono, nombre = await _extraer_cliente(payload)
    nombre_corto = nombre.split()[0] if nombre else "te"
    empresa = NOMBRE_NEGOCIO or "Grupo Ideas"
    mensaje = WA_MSG_ORDEN_CONFIRMADA.format(
        nombre=nombre_corto, nro_orden=nro_orden, monto=monto_fmt or "—", empresa=empresa,
    )

    enviado = await _notificar_orden(
        telefono, mensaje, order_id,
        nota_exito=f"✅ WhatsApp enviado al cliente (plantilla presupuesto_2):\n{mensaje}",
        plantilla=("presupuesto_2", "es_AR", [nombre_corto, nro_orden, empresa, moneda_simbolo, monto_numero]),
    )
    return {"ok": enviado, "telefono": telefono, "orden": nro_orden}


@router.post("/odoo/webhook/trabajo-listo")
async def webhook_trabajo_listo(request: Request):
    """Dispara cuando la tarea asociada a la orden pasa a estado 'listo'."""
    await verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook trabajo-listo payload: %s", payload)

    # La tarea puede traer el nro de orden en sale_order_id.name o nro_orden
    sale_order = payload.get("sale_order_id")
    nro_orden = (
        payload.get("nro_orden")
        or payload.get("name")
        or (sale_order.get("name") if isinstance(sale_order, dict) else None)
    )

    order_id = _resolver_order_id(payload)

    # El selector de campos del Webhook nativo de Odoo es limitado: completamos
    # por RPC si falta el número de orden o el cliente, siempre que tengamos el id.
    if (not nro_orden or not payload.get("partner_id")) and order_id:
        orden = await odoo_crm.buscar_orden_por_id(order_id)
        if orden:
            nro_orden = nro_orden or orden.get("name")
            if not payload.get("partner_id") and orden.get("partner_id"):
                payload["partner_id"] = orden["partner_id"]

    nro_orden = nro_orden or "—"

    telefono, nombre = await _extraer_cliente(payload)
    nombre_corto = nombre.split()[0] if nombre else "te"
    mensaje = WA_MSG_TRABAJO_LISTO.format(nombre=nombre_corto, nro_orden=nro_orden)

    enviado = await _notificar_orden(
        telefono, mensaje, order_id,
        nota_exito="✅ Cliente notificado: trabajo listo",
    )
    return {"ok": enviado, "telefono": telefono, "orden": nro_orden}


async def _procesar_trabajo_listo_sucursal(
    request: Request, nombre_plantilla: str, idioma: str, direccion: str, alias_texto: str,
) -> dict:
    """Común a los webhooks de trabajo-listo por sucursal (Taller/Oficina) —
    cada sucursal tiene su propia plantilla de Meta, dirección y alias de cobro."""
    await verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook trabajo-listo (%s) payload: %s", nombre_plantilla, payload)

    sale_order = payload.get("sale_order_id")
    nro_orden = (
        payload.get("nro_orden")
        or payload.get("name")
        or (sale_order.get("name") if isinstance(sale_order, dict) else None)
    )
    order_id = _resolver_order_id(payload)
    monto = payload.get("amount_total")

    if (not nro_orden or monto is None or not payload.get("partner_id")) and order_id:
        orden = await odoo_crm.buscar_orden_por_id(order_id)
        if orden:
            nro_orden = nro_orden or orden.get("name")
            monto = monto if monto is not None else orden.get("amount_total")
            if not payload.get("partner_id") and orden.get("partner_id"):
                payload["partner_id"] = orden["partner_id"]

    nro_orden = nro_orden or "—"
    monto_fmt = _formatear_monto(monto)
    moneda_simbolo, monto_numero = (monto_fmt[0], monto_fmt[1:]) if monto_fmt else ("$", "—")

    telefono, nombre = await _extraer_cliente(payload)
    nombre_corto = nombre.split()[0] if nombre else "te"

    mensaje = (
        f"Hola {nombre_corto},\n\n"
        f"Tu trabajo *{nro_orden}* ya está listo, podés pasar a retirarlo por {direccion}.\n\n"
        f"Total de *{moneda_simbolo}{monto_numero}*.\n\n"
        f"Alias :   {alias_texto}\n"
        "Enviar comprobante por favor.\n\n"
        "Gracias."
    )

    enviado = await _notificar_orden(
        telefono, mensaje, order_id,
        nota_exito=f"✅ WhatsApp enviado al cliente (plantilla {nombre_plantilla}):\n{mensaje}",
        plantilla=(nombre_plantilla, idioma, [nombre_corto, nro_orden, monto_numero, moneda_simbolo]),
    )
    return {"ok": enviado, "telefono": telefono, "orden": nro_orden}


@router.post("/odoo/webhook/trabajo-listo-taller")
async def webhook_trabajo_listo_taller(request: Request):
    """Trabajo listo para retirar en el Taller (16 de julio 980)."""
    return await _procesar_trabajo_listo_sucursal(
        request, WA_PLANTILLA_TRABAJO_LISTO_TALLER, "es_AR",
        "16 de julio 980 (Taller)", "Vhbertoli.mp - Víctor Hugo Bertoli (taller)",
    )


@router.post("/odoo/webhook/trabajo-listo-oficina")
async def webhook_trabajo_listo_oficina(request: Request):
    """Trabajo listo para retirar en la Oficina (9 de julio 194)."""
    return await _procesar_trabajo_listo_sucursal(
        request, WA_PLANTILLA_TRABAJO_LISTO_OFICINA, "es_AR",
        "9 de julio 194", "*Gideas.oficina* - Clelia Fernández (oficina)",
    )


@router.post("/odoo/enviar")
async def enviar_desde_odoo(request: Request):
    """Recibe llamadas desde Odoo para enviar mensajes por WhatsApp."""
    api_key = request.headers.get("X-Api-Key", "")
    if not BRIDGE_API_KEY or api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")

    body = await request.json()
    telefono  = "".join(c for c in body.get("telefono", "") if c.isdigit())
    nro_orden = body.get("nro_orden", "").strip()
    mensaje   = body.get("mensaje", "").strip()

    if not telefono:
        raise HTTPException(status_code=400, detail="telefono es requerido")
    if not mensaje and not nro_orden:
        raise HTTPException(status_code=400, detail="mensaje o nro_orden es requerido")

    if not mensaje:
        mensaje = f"Hola 👋 Te informamos que tu pedido *#{nro_orden}* ya está listo. ¡Gracias por elegirnos!"

    async with httpx.AsyncClient() as client:
        ok = await whatsapp.enviar_mensaje(client, telefono, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje por WhatsApp")

    canonical = await obtener_canonical_id(telefono)
    await guardar_mensaje(canonical, "assistant", f"[Odoo] {mensaje}")
    log.info("Odoo → WA enviado a %s | orden: %s", telefono, nro_orden or "—")
    return {"ok": True, "telefono": telefono}
