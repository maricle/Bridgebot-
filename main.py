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
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

import instagram
import whatsapp
from auth import verificar_api_key
from config import EXCLUIR_BOT, IG_ACCOUNT_ID, VERIFY_TOKEN
from db import (buscar_cliente_odoo_por_telefono, conversacion_cerrada,
                es_usuario_nuevo, guardar_archivo, guardar_datos_cliente,
                guardar_mensaje, init_db, limpiar_historial, marcar_comprobante,
                marcar_mensaje_procesado, marcar_saludado, mensaje_ya_procesado,
                obtener_canonical_id, resetear_cerrada, usuario_pausado)
from ai import generar_respuesta
from routers import dashboard_api
from routers import debug as debug_router
from routers import webhooks_odoo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Imagen/sticker sueltos (sin pedido) → se guardan pero no se confirma por mensaje
_TIPOS_MEDIA_SIN_RESPUESTA = {"image", "sticker"}


async def _analizar_pdf_comprobante(media_id: str) -> dict | None:
    """Si el PDF recibido por WhatsApp tiene pinta de comprobante de pago,
    devuelve los datos extraídos por Claude. None si no aplica o algo falla —
    nunca debe frenar el flujo normal de recepción del archivo."""
    if not media_id:
        return None
    try:
        async with httpx.AsyncClient() as client:
            contenido = await whatsapp.descargar_media(client, media_id)
        if not contenido:
            return None
        from comprobantes import analizar_comprobante, extraer_texto_pdf
        texto = extraer_texto_pdf(contenido)
        if not texto:
            return None
        return await analizar_comprobante(texto)
    except Exception as e:
        log.error("Error analizando PDF como comprobante: %s", e)
        return None

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
app.include_router(debug_router.router)
app.include_router(webhooks_odoo.router)
app.include_router(dashboard_api.router)


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
                hay_comprobante = False
                for arch in archivos:
                    archivo_id = await guardar_archivo(canonical, "whatsapp", arch["tipo"], media_id=arch.get("media_id", ""))

                    datos_comprobante = None
                    if arch.get("mime_type") == "application/pdf":
                        datos_comprobante = await _analizar_pdf_comprobante(arch.get("media_id", ""))
                    documento_sin_clasificar = arch["tipo"] == "document" and not datos_comprobante

                    await guardar_mensaje(
                        canonical, "user",
                        f"[Archivo recibido: {arch['tipo']}] /archivos/{archivo_id}/descargar",
                        notificar_odoo=not datos_comprobante and not documento_sin_clasificar,
                    )

                    if datos_comprobante:
                        import json as _json
                        from odoo_crm import notificar_comprobante_pago
                        await marcar_comprobante(archivo_id, _json.dumps(datos_comprobante, ensure_ascii=False))
                        asyncio.create_task(notificar_comprobante_pago(canonical, datos_comprobante))
                        hay_comprobante = True
                    elif documento_sin_clasificar:
                        from odoo_crm import notificar_documento_recibido
                        asyncio.create_task(notificar_documento_recibido(canonical))

                log.info("WA: %s archivo(s) guardado(s) para %s", len(archivos), sender_arch)
                if hay_comprobante:
                    async with httpx.AsyncClient() as client:
                        await whatsapp.enviar_mensaje(client, sender_arch, "¡Recibimos tu comprobante de pago! 🙌 Ya quedó registrado.")
                elif any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
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
