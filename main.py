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
from config import (BRIDGE_API_KEY, EXCLUIR_BOT, IG_ACCOUNT_ID,
                    VERIFY_TOKEN, WA_MSG_ORDEN_CONFIRMADA, WA_MSG_TRABAJO_LISTO,
                    WA_PLANTILLA_TRABAJO_LISTO_OFICINA, WA_PLANTILLA_TRABAJO_LISTO_TALLER)
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
    import config as _config
    from config import ANTHROPIC_API_KEY
    from precios import cargar as cargar_precios
    await init_db()
    await _config.recargar_configuracion()
    await _config.recargar_conocimiento()
    await cargar_precios()
    t1 = asyncio.create_task(_refresh_precios_loop())
    t2 = asyncio.create_task(_sync_clientes_loop())
    t3 = asyncio.create_task(_sync_tareas_loop())
    modo = "AUTO_RESPUESTA" if _config.AUTO_RESPUESTA else "CLAUDE"
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
                    archivo_id = await guardar_archivo(canonical, "instagram", arch["tipo"], url=arch.get("url", ""))
                    await guardar_mensaje(canonical, "user",
                        f"[Archivo recibido: {arch['tipo']}] /archivos/{archivo_id}/descargar")
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
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return
        if await usuario_pausado(sender_id):
            log.info("IG: bot pausado para %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
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
                from config import AUTO_RESPUESTA, SALUDO
                if AUTO_RESPUESTA:
                    canonical = await obtener_canonical_id(sender_id)
                    await guardar_mensaje(canonical, "user", mensaje)
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
                    archivo_id = await guardar_archivo(canonical, "whatsapp", arch["tipo"], media_id=arch.get("media_id", ""))
                    await guardar_mensaje(canonical, "user",
                        f"[Archivo recibido: {arch['tipo']}] /archivos/{archivo_id}/descargar")
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
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return
        if await usuario_pausado(sender_id):
            log.info("WA: bot pausado para %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
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
                from config import AUTO_RESPUESTA, SALUDO
                if AUTO_RESPUESTA:
                    canonical = await obtener_canonical_id(sender_id)
                    await guardar_mensaje(canonical, "user", mensaje)
                    if cerrada or await es_usuario_nuevo(sender_id):
                        await whatsapp.enviar_mensaje(client, sender_id, SALUDO)
                        if not cerrada:
                            await marcar_saludado(sender_id, "whatsapp")
                    return

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


@app.get("/sync-clientes")
async def sync_clientes_manual():
    """Fuerza la sincronización de clientes de Odoo (misma que corre cada 24h)."""
    from odoo_crm import sincronizar_clientes
    from db import upsert_clientes_odoo
    clientes = await sincronizar_clientes()
    if clientes:
        await upsert_clientes_odoo(clientes)
    return {"ok": True, "clientes_sincronizados": len(clientes)}


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
    from config import AUTO_RESPUESTA, WA_ACCESS_TOKEN, WA_PHONE_ID
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
    base_dir = os.path.dirname(__file__)
    with open(os.path.join(base_dir, "static", "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    # Cache-busting: el navegador cachea agresivamente los estaticos servidos por
    # StaticFiles. Sin esto, despues de cada deploy los usuarios con la pestaña ya
    # abierta (o cache reciente) siguen viendo el dashboard.js/css viejo.
    for nombre in ("dashboard.css", "dashboard.js"):
        version = int(os.path.getmtime(os.path.join(base_dir, "static", nombre)))
        html = html.replace(f"/static/{nombre}", f"/static/{nombre}?v={version}")
    return HTMLResponse(html)


@app.get("/dashboard-config")
async def dashboard_config():
    from config import DASHBOARD_COLOR, NOMBRE_NEGOCIO
    return {"nombre": NOMBRE_NEGOCIO, "color": DASHBOARD_COLOR}


_CONFIG_CLAVES = {
    "SALUDO_BIENVENIDA", "AUTO_RESPUESTA", "ALIAS_TRANSFERENCIA",
    "NOMBRE_NEGOCIO", "DASHBOARD_COLOR",
}


@app.get("/config")
async def obtener_configuracion():
    from config import (ALIAS_TRANSFERENCIA, AUTO_RESPUESTA, DASHBOARD_COLOR,
                        NOMBRE_NEGOCIO, SALUDO)
    return {
        "SALUDO_BIENVENIDA": SALUDO,
        "AUTO_RESPUESTA": AUTO_RESPUESTA,
        "ALIAS_TRANSFERENCIA": ALIAS_TRANSFERENCIA,
        "NOMBRE_NEGOCIO": NOMBRE_NEGOCIO,
        "DASHBOARD_COLOR": DASHBOARD_COLOR,
    }


@app.post("/config")
async def guardar_configuracion(request: Request):
    await _verificar_api_key(request)
    body = await request.json()
    from db import guardar_config
    for clave in _CONFIG_CLAVES:
        if clave in body:
            await guardar_config(f"config:{clave}", str(body[clave]))
    import config as _config
    await _config.recargar_configuracion()
    return {"ok": True}


@app.get("/config/knowledge")
async def obtener_knowledge():
    import config as _config
    return await _config.obtener_knowledge_efectivo()


@app.post("/config/knowledge/{archivo}")
async def guardar_knowledge(archivo: str, request: Request):
    import config as _config
    if archivo not in _config.KNOWLEDGE_ARCHIVOS:
        raise HTTPException(status_code=400, detail="Archivo no permitido")
    await _verificar_api_key(request)
    body = await request.json()
    contenido = body.get("contenido", "")

    from db import guardar_config
    await guardar_config(f"knowledge:{archivo}", contenido)

    if archivo == "precios.md":
        from precios import cargar as cargar_precios
        await cargar_precios()
    else:
        await _config.recargar_conocimiento()
    return {"ok": True}


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
async def historial_reciente(limite: int = 20, offset: int = 0, canal: str = ""):
    return await obtener_conversaciones_recientes(limite=limite, offset=offset, canal=canal)


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
    # Si BRIDGE_API_KEY no está configurada, se omite la validación (modo dev).
    if not BRIDGE_API_KEY:
        return
    api_key = request.headers.get("X-Api-Key", "") or request.query_params.get("key", "")
    if api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


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
        from odoo_crm import obtener_telefono_partner
        telefono = await obtener_telefono_partner(int(partner_id))

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
        from odoo_crm import registrar_nota
        await registrar_nota(odoo_model, odoo_id, f"WhatsApp enviado al cliente:\n{mensaje}")


async def _notificar_orden(
    telefono: str, mensaje: str, order_id: int | None, nota_exito: str,
    plantilla: tuple[str, str, list[str]] | None = None,
) -> bool:
    """Envía el WA y deja constancia en el chatter de la orden (éxito o motivo del fallo).

    Si se pasa `plantilla` (nombre, idioma, parametros) se envía como mensaje de
    plantilla de Meta en vez de texto libre — necesario fuera de la ventana de 24hs.

    Nunca lanza — el webhook de Odoo siempre debe recibir 200, de lo contrario
    Odoo reintenta el envío y se duplican los mensajes al cliente."""
    from odoo_crm import registrar_nota_orden

    if not telefono:
        log.warning("Sin teléfono registrado — no se envía WhatsApp (orden id=%s)", order_id)
        if order_id:
            await registrar_nota_orden(
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
            await registrar_nota_orden(order_id, nota_exito)
    else:
        log.error("No se pudo enviar WhatsApp a %s", telefono)
        if order_id:
            await registrar_nota_orden(
                order_id,
                "⚠️ No se pudo enviar WhatsApp: error al enviar el mensaje por WhatsApp.",
            )

    return enviado


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
    await _enviar_notificacion_wa(
        telefono, mensaje, nro_orden,
        odoo_model=str(payload.get("_model") or ""),
        odoo_id=int(payload["id"]) if payload.get("id") else 0,
    )
    return {"ok": True, "telefono": telefono, "orden": nro_orden}


@app.get("/whatsapp/plantillas")
async def whatsapp_plantillas(request: Request):
    """Debug: lista las plantillas de WhatsApp aprobadas en Meta para esta cuenta."""
    await _verificar_api_key(request)
    async with httpx.AsyncClient() as client:
        return await whatsapp.obtener_plantillas(client)


@app.post("/odoo/webhook/orden-confirmada")
async def webhook_orden_confirmada(request: Request):
    """Dispara cuando se confirma una orden de venta (sale.order state=sale)."""
    await _verificar_api_key(request)
    payload = await request.json()
    log.info("Odoo webhook orden-confirmada payload: %s", payload)

    order_id = _resolver_order_id(payload) or (int(payload["id"]) if payload.get("id") else None)
    nro_orden = payload.get("name") or payload.get("nro_orden") or "—"
    monto = payload.get("amount_total")
    access_url = payload.get("access_url") or ""

    # El selector de campos del Webhook nativo de Odoo es limitado: si no
    # vino el monto, el nombre o el link pero sí el id, lo completamos por RPC.
    if (monto is None or not nro_orden or nro_orden == "—" or not access_url) and order_id:
        from odoo_crm import buscar_orden_por_id
        orden = await buscar_orden_por_id(order_id)
        if orden:
            nro_orden = nro_orden if nro_orden and nro_orden != "—" else orden.get("name") or "—"
            monto = monto if monto is not None else orden.get("amount_total")
            access_url = access_url or orden.get("access_url") or ""

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

    order_id = _resolver_order_id(payload)

    # El selector de campos del Webhook nativo de Odoo es limitado: si el
    # propio payload es una sale.order y no vino el número, lo buscamos por RPC.
    if not nro_orden and payload.get("_model") == "sale.order" and payload.get("id"):
        from odoo_crm import buscar_orden_por_id
        orden = await buscar_orden_por_id(int(payload["id"]))
        if orden:
            nro_orden = orden.get("name")

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
    await _verificar_api_key(request)
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

    if (not nro_orden or monto is None) and order_id:
        from odoo_crm import buscar_orden_por_id
        orden = await buscar_orden_por_id(order_id)
        if orden:
            nro_orden = nro_orden or orden.get("name")
            monto = monto if monto is not None else orden.get("amount_total")

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


@app.post("/odoo/webhook/trabajo-listo-taller")
async def webhook_trabajo_listo_taller(request: Request):
    """Trabajo listo para retirar en el Taller (16 de julio 980)."""
    return await _procesar_trabajo_listo_sucursal(
        request, WA_PLANTILLA_TRABAJO_LISTO_TALLER, "es_AR",
        "16 de julio 980 (Taller)", "Vhbertoli.mp - Víctor Hugo Bertoli (taller)",
    )


@app.post("/odoo/webhook/trabajo-listo-oficina")
async def webhook_trabajo_listo_oficina(request: Request):
    """Trabajo listo para retirar en la Oficina (9 de julio 194)."""
    return await _procesar_trabajo_listo_sucursal(
        request, WA_PLANTILLA_TRABAJO_LISTO_OFICINA, "es_AR",
        "9 de julio 194", "*Gideas.oficina* - Clelia Fernández (oficina)",
    )


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
    canal = body.get("canal", "").strip()
    if not user_id or not mensaje:
        raise HTTPException(status_code=400, detail="user_id y mensaje son requeridos")

    if not canal:
        datos = await obtener_datos_cliente(user_id)
        canal = datos.get("canal", "")

    async with httpx.AsyncClient() as client:
        if canal == "instagram":
            ok = await instagram.enviar_mensaje(client, user_id, mensaje)
        else:
            ok = await whatsapp.enviar_mensaje(client, user_id, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Error enviando mensaje por {canal or 'WhatsApp'}")
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
