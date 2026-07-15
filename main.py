"""
BridgeBot — Instagram + WhatsApp → Claude AI Agent
Kleba Dev — 2026
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import instagram
import whatsapp
from config import (AUTO_RESPUESTA, BRIDGE_API_KEY, EXCLUIR_BOT, IG_ACCOUNT_ID,
                    SALUDO, VERIFY_TOKEN, WA_MSG_ORDEN_CONFIRMADA, WA_MSG_TRABAJO_LISTO)
from db import (buscar_cliente_odoo_por_id, buscar_cliente_odoo_por_telefono,
                buscar_en_historial, buscar_usuario_por_telefono,
                conversacion_cerrada, es_usuario_nuevo, guardar_archivo,
                guardar_datos_cliente, guardar_mensaje, init_db, listar_archivos,
                limpiar_historial, marcar_mensaje_procesado, marcar_saludado,
                mensaje_ya_procesado, obtener_archivo_por_id, obtener_canonical_id,
                obtener_conversacion, obtener_conversaciones_recientes,
                obtener_datos_cliente, obtener_leads,
                obtener_usuarios, pausar_usuario, reanudar_usuario, resetear_cerrada,
                resetear_usuario, stats, usuario_pausado)
from ai import generar_respuesta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Imagen/sticker sueltos (sin pedido) → se guardan pero no se confirma por mensaje
_TIPOS_MEDIA_SIN_RESPUESTA = {"image", "sticker"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    from config import ANTHROPIC_API_KEY
    from precios import cargar as cargar_precios
    await init_db()
    await cargar_precios()
    t1 = asyncio.create_task(_refresh_precios_loop())
    t2 = asyncio.create_task(_sync_clientes_loop())
    t3 = asyncio.create_task(_sync_tareas_loop())
    modo = "AUTO_RESPUESTA" if AUTO_RESPUESTA else "CLAUDE"
    log.info("BridgeBot v5 iniciado — modo: %s", modo)
    log.info("Claude configurado: %s", "SI" if ANTHROPIC_API_KEY else "NO")
    yield
    t1.cancel()
    t2.cancel()
    t3.cancel()


async def _refresh_precios_loop():
    from precios import cargar as cargar_precios
    while True:
        await asyncio.sleep(86400)
        await cargar_precios()
        log.info("Precios actualizados automáticamente")
        inactivos = [uid for uid, lock in list(_user_locks.items()) if not lock.locked()]
        for uid in inactivos:
            _user_locks.pop(uid, None)
        if inactivos:
            log.info("Limpieza locks usuarios: %d eliminados", len(inactivos))


async def _sync_clientes_loop():
    from odoo_crm import sincronizar_clientes
    from db import upsert_clientes_odoo
    await asyncio.sleep(60)  # esperar que la app arranque
    while True:
        clientes = await sincronizar_clientes()
        if clientes:
            await upsert_clientes_odoo(clientes)
        await asyncio.sleep(86400)  # repetir cada 24h


async def _sync_tareas_loop():
    from odoo_crm import sincronizar_tareas
    from db import upsert_tareas_odoo
    await asyncio.sleep(120)  # arrancar 2 min después del inicio
    while True:
        tareas = await sincronizar_tareas()
        if tareas:
            await upsert_tareas_odoo(tareas)
        await asyncio.sleep(1800)  # repetir cada 30 min


app = FastAPI(title="BridgeBot", version="5.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook")
async def recibir_webhook(request: Request):
    payload = await request.body()
    firma   = request.headers.get("X-Hub-Signature-256", "")
    if not instagram.verificar_firma(payload, firma):
        raise HTTPException(status_code=401, detail="Firma inválida")
    data   = await request.json()
    objeto = data.get("object", "")

    if objeto == "instagram":
        entry = data.get("entry", [{}])[0]
        log.info("IG webhook entry=%s", entry.get("id", "?"))
        asyncio.create_task(procesar_instagram(data))
    elif objeto == "whatsapp_business_account":
        log.info("WA webhook recibido via /webhook")
        asyncio.create_task(procesar_whatsapp(data))
    else:
        log.info("Webhook objeto desconocido: %s", objeto)

    return Response(status_code=200)


async def procesar_instagram(data: dict):
    try:
        # Archivos adjuntos
        sender_arch, archivos = instagram.extraer_archivos(data)
        if sender_arch and archivos:
            async with _user_locks[sender_arch]:
                canonical = await obtener_canonical_id(sender_arch)
                for arch in archivos:
                    await guardar_archivo(canonical, "instagram", arch["tipo"], url=arch.get("url", ""))
                log.info("IG: %s archivo(s) guardado(s) para %s", len(archivos), sender_arch)
                if any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
                    async with httpx.AsyncClient() as client:
                        await instagram.enviar_mensaje(client, sender_arch, "¡Recibimos el archivo! Lo vamos a adjuntar al pedido.")
            return

        sender_id, mensaje = instagram.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("IG: evento sin texto, ignorando.")
            return
        if sender_id == IG_ACCOUNT_ID:
            log.info("IG: mensaje propio, ignorando.")
            return
        if sender_id in EXCLUIR_BOT:
            log.info("IG: atendido por humano %s, ignorando.", sender_id)
            return
        if await usuario_pausado(sender_id):
            log.info("IG: bot pausado para %s, ignorando.", sender_id)
            return

        # Deduplicación (antes del lock, después de filtros)
        message_id = instagram.extraer_message_id(data)
        if message_id:
            if await mensaje_ya_procesado(message_id):
                log.info("IG: mensaje duplicado ignorado: %s", message_id)
                return
            await marcar_mensaje_procesado(message_id)

        async with _user_locks[sender_id]:
            cerrada = await conversacion_cerrada(sender_id)
            if cerrada:
                await resetear_cerrada(sender_id)
                canonical = await obtener_canonical_id(sender_id)
                await limpiar_historial(canonical)
                log.info("IG: conversación cerrada reseteada para %s — procesando mensaje con Claude", sender_id)

            log.info("IG user=%s: %s", sender_id, mensaje[:100])
            async with httpx.AsyncClient() as client:
                if AUTO_RESPUESTA:
                    if cerrada or await es_usuario_nuevo(sender_id):
                        await instagram.enviar_mensaje(client, sender_id, SALUDO)
                        if not cerrada:
                            await marcar_saludado(sender_id, "instagram")
                    return

                nuevo = await es_usuario_nuevo(sender_id)
                if nuevo:
                    await marcar_saludado(sender_id, "instagram")
                respuesta = await generar_respuesta(sender_id, mensaje, "instagram", es_nuevo=(cerrada or nuevo))
                if respuesta:
                    await instagram.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("IG error procesando evento: %s", e)


# ─── WHATSAPP ─────────────────────────────────────────────────────────────────

@app.get("/webhook/whatsapp")
async def verificar_webhook_wa(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook WA verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook/whatsapp")
async def recibir_whatsapp(request: Request):
    data = await request.json()
    log.info("WA webhook recibido: %s", str(data)[:200])
    asyncio.create_task(procesar_whatsapp(data))
    return Response(status_code=200)


async def procesar_whatsapp(data: dict):
    try:
        # Deduplicación: Meta reintenta el webhook si no recibe 200 a tiempo
        message_id = whatsapp.extraer_message_id(data)
        if message_id:
            if await mensaje_ya_procesado(message_id):
                log.info("WA: mensaje duplicado ignorado: %s", message_id)
                return
            await marcar_mensaje_procesado(message_id)

        # Archivos adjuntos
        sender_arch, archivos = whatsapp.extraer_archivos(data)
        if sender_arch and archivos:
            async with _user_locks[sender_arch]:
                canonical = await obtener_canonical_id(sender_arch)
                for arch in archivos:
                    await guardar_archivo(canonical, "whatsapp", arch["tipo"], media_id=arch.get("media_id", ""))
                log.info("WA: %s archivo(s) guardado(s) para %s", len(archivos), sender_arch)
                if any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
                    async with httpx.AsyncClient() as client:
                        await whatsapp.enviar_mensaje(client, sender_arch, "¡Recibimos el archivo! Lo vamos a adjuntar al pedido.")
            return

        sender_id, mensaje = whatsapp.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("WA: evento sin texto, ignorando.")
            return
        if sender_id in EXCLUIR_BOT:
            log.info("WA: atendido por humano %s, ignorando.", sender_id)
            return
        if await usuario_pausado(sender_id):
            log.info("WA: bot pausado para %s, ignorando.", sender_id)
            return

        async with _user_locks[sender_id]:
            cerrada = await conversacion_cerrada(sender_id)
            if cerrada:
                await resetear_cerrada(sender_id)
                canonical = await obtener_canonical_id(sender_id)
                await limpiar_historial(canonical)
                log.info("WA: conversación cerrada reseteada para %s — procesando mensaje con Claude", sender_id)

            log.info("WA user=%s: %s", sender_id, mensaje[:100])
            async with httpx.AsyncClient() as client:
                nuevo = await es_usuario_nuevo(sender_id)
                if nuevo:
                    await marcar_saludado(sender_id, "whatsapp")
                    odoo_match = await buscar_cliente_odoo_por_telefono(sender_id)
                    if odoo_match and odoo_match.get("nombre"):
                        await guardar_datos_cliente(sender_id, nombre=odoo_match["nombre"],
                                                    email=odoo_match.get("email") or "")
                        log.info("WA: cliente Odoo identificado para %s (%s)", sender_id, odoo_match["nombre"])
                respuesta = await generar_respuesta(sender_id, mensaje, "whatsapp", es_nuevo=(cerrada or nuevo))
                if respuesta:
                    await whatsapp.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("WA error procesando evento: %s", e)


# ─── UTILS ────────────────────────────────────────────────────────────────────

@app.get("/test-claude")
async def test_claude():
    from config import ANTHROPIC_API_KEY
    from ai import _llamar_claude
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY no configurada"}
    respuesta = await _llamar_claude(
        messages=[{"role": "user", "content": "Respondé solo: hola"}],
        max_tokens=50,
    )
    if respuesta:
        return {"ok": True, "respuesta": respuesta}
    return {"ok": False, "error": "Claude no respondió — revisá los logs"}


@app.get("/test-odoo")
async def test_odoo():
    from config import ODOO_API_KEY, ODOO_URL, ODOO_LOGIN

    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        return {
            "ok": False,
            "error": "Variables faltantes",
            "ODOO_URL": ODOO_URL or "VACÍO",
            "ODOO_API_KEY": f"{ODOO_API_KEY[:6]}..." if ODOO_API_KEY else "VACÍO",
            "ODOO_LOGIN": ODOO_LOGIN or "VACÍO",
        }

    from odoo_crm import crear_lead
    lead_id = await crear_lead(
        nombre_cliente="Test BridgeBot",
        telefono="0000000000",
        descripcion="Lead de prueba — podés eliminarlo.",
        canal="test",
        user_id="test",
    )
    if lead_id:
        return {"ok": True, "odoo_lead_id": lead_id}
    return {"ok": False, "mensaje": "Revisá los logs de Railway para ver el error exacto"}


@app.get("/sync-tareas")
async def sync_tareas_manual():
    from odoo_crm import sincronizar_tareas
    from db import upsert_tareas_odoo
    tareas = await sincronizar_tareas()
    if tareas:
        await upsert_tareas_odoo(tareas)
    return {"ok": True, "tareas_sincronizadas": len(tareas)}


@app.get("/actualizar-precios")
async def actualizar_precios():
    from precios import cargar as cargar_precios, obtener
    await cargar_precios()
    contenido = obtener()
    return {
        "ok": True,
        "chars": len(contenido),
        "preview": contenido[:200] + "..." if len(contenido) > 200 else contenido,
    }


@app.get("/health")
async def health():
    from config import WA_ACCESS_TOKEN, WA_PHONE_ID
    wa_ok = False
    wa_numero = None
    if WA_ACCESS_TOKEN and WA_PHONE_ID:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}",
                    params={"access_token": WA_ACCESS_TOKEN},
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    wa_ok = True
                    wa_numero = data.get("display_phone_number")
        except Exception:
            pass
    return {
        "status": "ok",
        "version": "5.0.0",
        "modo": "AUTO_RESPUESTA" if AUTO_RESPUESTA else "CLAUDE",
        "whatsapp": {"ok": wa_ok, "numero": wa_numero},
        **(await stats()),
    }


@app.get("/dashboard")
async def dashboard():
    import os
    html_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/dashboard-config")
async def dashboard_config():
    from config import DASHBOARD_COLOR, NOMBRE_NEGOCIO
    return {"nombre": NOMBRE_NEGOCIO, "color": DASHBOARD_COLOR}


@app.get("/analytics")
async def analytics(desde: str = "", hasta: str = ""):
    from datetime import date, timedelta
    from analytics import obtener_analytics
    if not hasta:
        hasta = date.today().isoformat()
    if not desde:
        desde = (date.today() - timedelta(days=30)).isoformat()
    return await obtener_analytics(desde, hasta)


@app.get("/leads")
async def ver_leads():
    return await obtener_leads()


@app.get("/usuarios")
async def ver_usuarios():
    return await obtener_usuarios()


@app.get("/conversacion/{user_id}")
async def ver_conversacion(user_id: str):
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


@app.get("/buscar-contenido")
async def buscar_por_contenido(q: str):
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Texto de búsqueda muy corto")
    resultados = await buscar_en_historial(q.strip())
    return resultados


@app.get("/historial-reciente")
async def historial_reciente(limite: int = 20, offset: int = 0):
    return await obtener_conversaciones_recientes(limite=limite, offset=offset)


@app.get("/buscar")
async def buscar_por_telefono(telefono: str):
    user_id = await buscar_usuario_por_telefono(telefono)
    if not user_id:
        return {"encontrado": False, "user_id": None, "cliente": {}, "historial": []}
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"encontrado": True, "user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


async def _verificar_api_key(request: Request):
    # Header X-Api-Key (integraciones normales) o ?key=... en la URL
    # (la acción "Webhook" nativa de Odoo no permite configurar headers custom).
    api_key = request.headers.get("X-Api-Key", "") or request.query_params.get("key", "")
    if not BRIDGE_API_KEY or api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


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

    return telefono, nombre


async def _enviar_notificacion_wa(telefono: str, mensaje: str, nro_orden: str):
    async with httpx.AsyncClient() as client:
        ok = await whatsapp.enviar_mensaje(client, telefono, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje por WhatsApp")
    canonical = await obtener_canonical_id(telefono)
    await guardar_mensaje(canonical, "assistant", f"[Odoo] {mensaje}")
    log.info("Odoo → WA enviado a %s | orden: %s", telefono, nro_orden)


@app.post("/odoo/webhook")
async def webhook_odoo(request: Request):
    """Endpoint genérico — mantiene compatibilidad con la configuración anterior."""
    await _verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook (genérico) payload: %s", payload)
    nro_orden = payload.get("name") or payload.get("display_name") or "—"
    telefono, nombre = await _extraer_cliente(payload)
    if not telefono:
        return {"ok": False, "detalle": f"Sin teléfono para orden {nro_orden}"}
    nombre_corto = nombre.split()[0] if nombre else "te"
    mensaje = payload.get("mensaje") or WA_MSG_TRABAJO_LISTO.format(nombre=nombre_corto, nro_orden=nro_orden)
    await _enviar_notificacion_wa(telefono, mensaje, nro_orden)
    return {"ok": True, "telefono": telefono, "orden": nro_orden}


@app.post("/odoo/webhook/orden-confirmada")
async def webhook_orden_confirmada(request: Request):
    """Dispara cuando se confirma una orden de venta (sale.order state=sale)."""
    await _verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook orden-confirmada payload: %s", payload)

    nro_orden = payload.get("name") or payload.get("nro_orden") or "—"
    telefono, nombre = await _extraer_cliente(payload)

    if not telefono:
        log.warning("orden-confirmada: sin teléfono para orden %s", nro_orden)
        return {"ok": False, "detalle": f"Sin teléfono para orden {nro_orden}"}

    nombre_corto = nombre.split()[0] if nombre else "te"
    mensaje = WA_MSG_ORDEN_CONFIRMADA.format(nombre=nombre_corto, nro_orden=nro_orden)
    await _enviar_notificacion_wa(telefono, mensaje, nro_orden)
    return {"ok": True, "telefono": telefono, "orden": nro_orden}


@app.post("/odoo/webhook/trabajo-listo")
async def webhook_trabajo_listo(request: Request):
    """Dispara cuando la tarea asociada a la orden pasa a estado 'listo'."""
    await _verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook trabajo-listo payload: %s", payload)

    # La tarea puede traer el nro de orden en sale_order_id.name o nro_orden
    sale_order = payload.get("sale_order_id")
    nro_orden = (
        payload.get("nro_orden")
        or payload.get("name")
        or (sale_order.get("name") if isinstance(sale_order, dict) else None)
    )

    # El selector de campos del Webhook nativo de Odoo es limitado: si el
    # propio payload es una sale.order y no vino el número, lo buscamos por RPC.
    if not nro_orden and payload.get("_model") == "sale.order" and payload.get("id"):
        from odoo_crm import buscar_orden_por_id
        orden = await buscar_orden_por_id(int(payload["id"]))
        if orden:
            nro_orden = orden.get("name")

    nro_orden = nro_orden or "—"

    telefono, nombre = await _extraer_cliente(payload)

    if not telefono:
        log.warning("trabajo-listo: sin teléfono para orden %s", nro_orden)
        return {"ok": False, "detalle": f"Sin teléfono para orden {nro_orden}"}

    nombre_corto = nombre.split()[0] if nombre else "te"
    mensaje = WA_MSG_TRABAJO_LISTO.format(nombre=nombre_corto, nro_orden=nro_orden)
    await _enviar_notificacion_wa(telefono, mensaje, nro_orden)
    return {"ok": True, "telefono": telefono, "orden": nro_orden}


@app.post("/odoo/enviar")
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


@app.post("/responder")
async def responder_whatsapp(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    mensaje = body.get("mensaje", "").strip()
    if not user_id or not mensaje:
        raise HTTPException(status_code=400, detail="user_id y mensaje son requeridos")
    async with httpx.AsyncClient() as client:
        ok = await whatsapp.enviar_mensaje(client, user_id, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail="Error enviando mensaje por WhatsApp")
    canonical = await obtener_canonical_id(user_id)
    await guardar_mensaje(canonical, "assistant", mensaje)
    return {"ok": True}


@app.get("/archivos")
async def ver_archivos():
    return await listar_archivos()


@app.get("/archivos/{archivo_id}/descargar")
async def descargar_archivo(archivo_id: int):
    from fastapi.responses import StreamingResponse
    from config import WA_ACCESS_TOKEN
    archivo = await obtener_archivo_por_id(archivo_id)
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    media_id = archivo.get("media_id", "")
    url_directa = archivo.get("url", "")

    headers_meta = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}

    async with httpx.AsyncClient() as client:
        # WA: refrescar URL via media_id
        if media_id and not url_directa:
            info = await client.get(
                f"https://graph.facebook.com/v19.0/{media_id}",
                headers=headers_meta, timeout=10,
            )
            if info.status_code != 200:
                raise HTTPException(status_code=502, detail="No se pudo obtener la URL del archivo de Meta")
            url_directa = info.json().get("url", "")

        if not url_directa:
            raise HTTPException(status_code=404, detail="Sin URL disponible para este archivo")

        resp = await client.get(url_directa, headers=headers_meta, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Error descargando el archivo de Meta")

        content_type = resp.headers.get("content-type", "application/octet-stream")
        tipo = archivo.get("tipo", "archivo")
        ext = content_type.split("/")[-1].split(";")[0]
        filename = f"{tipo}_{archivo_id}.{ext}"

        return StreamingResponse(
            iter([resp.content]),
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@app.delete("/usuario/{user_id}")
async def borrar_usuario(user_id: str):
    await resetear_usuario(user_id)
    return {"ok": True, "mensaje": f"Usuario {user_id} reseteado"}


@app.post("/usuario/{user_id}/pausar")
async def pausar_usuario_endpoint(user_id: str):
    await pausar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot pausado para {user_id}"}


@app.post("/usuario/{user_id}/reanudar")
async def reanudar_usuario_endpoint(user_id: str):
    await reanudar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot reanudado para {user_id}"}
