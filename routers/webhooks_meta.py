"""Webhooks de Meta (Instagram + WhatsApp) — recepción de mensajes/archivos de
clientes y el flujo de respuesta vía Claude. Es el flujo de mayor riesgo de
negocio (conversación real con clientes en vivo)."""

import asyncio
import json
import logging
from collections import defaultdict

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

import instagram
import odoo_crm
import whatsapp
from ai import generar_respuesta
from comprobantes import analizar_comprobante, extraer_texto_pdf
from config import EXCLUIR_BOT, IG_ACCOUNT_ID, VERIFY_TOKEN
from db import (buscar_cliente_odoo_por_telefono, conversacion_cerrada,
                es_usuario_nuevo, guardar_archivo, guardar_datos_cliente,
                guardar_mensaje, limpiar_historial, marcar_comprobante,
                marcar_mensaje_procesado, marcar_saludado, mensaje_ya_procesado,
                obtener_canonical_id, resetear_cerrada, usuario_pausado)

log = logging.getLogger(__name__)
router = APIRouter()

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
        texto = extraer_texto_pdf(contenido)
        if not texto:
            return None
        return await analizar_comprobante(texto)
    except Exception as e:
        log.error("Error analizando PDF como comprobante: %s", e)
        return None


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

@router.get("/webhook")
async def verificar_webhook(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@router.post("/webhook")
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

@router.get("/webhook/whatsapp")
async def verificar_webhook_wa(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook WA verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@router.post("/webhook/whatsapp")
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
                        await marcar_comprobante(archivo_id, json.dumps(datos_comprobante, ensure_ascii=False))
                        asyncio.create_task(odoo_crm.notificar_comprobante_pago(canonical, datos_comprobante))
                        hay_comprobante = True
                    elif documento_sin_clasificar:
                        asyncio.create_task(odoo_crm.notificar_documento_recibido(canonical))

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
