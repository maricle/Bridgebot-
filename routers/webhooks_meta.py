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


class _Canal:
    """Adapta las diferencias reales entre Instagram y WhatsApp para que
    _procesar_mensaje_canal() tenga un solo pipeline. instagram/whatsapp
    exponen la misma interfaz (extraer_message_id/extraer_mensaje/
    extraer_archivos/enviar_mensaje), así que el módulo del canal se usa
    directo — no hace falta una clase base formal para eso."""

    def __init__(self, nombre: str, modulo, es_telefono: bool = False, filtro_propio=None):
        self.nombre = nombre
        self.modulo = modulo
        # True solo para WhatsApp: ahí sender_id es el teléfono real del
        # cliente, así que tiene sentido buscarlo en la sync de Odoo. El
        # sender_id de Instagram es un PSID interno de Meta — tratarlo como
        # teléfono generaría falsos positivos por coincidencia de dígitos.
        self.es_telefono = es_telefono
        self.filtro_propio = filtro_propio or (lambda sender_id: False)


_CANAL_INSTAGRAM = _Canal("instagram", instagram, filtro_propio=lambda sid: sid == IG_ACCOUNT_ID)
_CANAL_WHATSAPP = _Canal("whatsapp", whatsapp, es_telefono=True)


async def _procesar_mensaje_canal(data: dict, canal: _Canal):
    prefijo = "IG" if canal.nombre == "instagram" else "WA"
    modulo = canal.modulo
    try:
        # Deduplicación: Meta reintenta el webhook si no recibe 200 a tiempo
        message_id = modulo.extraer_message_id(data)
        if message_id:
            if await mensaje_ya_procesado(message_id):
                log.info("%s: mensaje duplicado ignorado: %s", prefijo, message_id)
                return
            await marcar_mensaje_procesado(message_id)

        # Archivos adjuntos
        sender_arch, archivos = modulo.extraer_archivos(data)
        if sender_arch and archivos:
            async with _user_locks[sender_arch]:
                canonical = await obtener_canonical_id(sender_arch)
                hay_comprobante = False
                for arch in archivos:
                    archivo_id = await guardar_archivo(
                        canonical, canal.nombre, arch["tipo"],
                        media_id=arch.get("media_id", ""), url=arch.get("url", ""),
                    )

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

                log.info("%s: %s archivo(s) guardado(s) para %s", prefijo, len(archivos), sender_arch)
                if hay_comprobante:
                    texto_confirmacion = "¡Recibimos tu comprobante de pago! 🙌 Ya quedó registrado."
                elif any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
                    texto_confirmacion = "¡Recibimos el archivo! Lo vamos a adjuntar al pedido."
                else:
                    texto_confirmacion = ""
                if texto_confirmacion:
                    async with httpx.AsyncClient() as client:
                        enviado = await modulo.enviar_mensaje(client, sender_arch, texto_confirmacion)
                    if enviado:
                        await guardar_mensaje(canonical, "assistant", texto_confirmacion)
            return

        sender_id, mensaje = modulo.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("%s: evento sin texto, ignorando.", prefijo)
            return
        if canal.filtro_propio(sender_id):
            log.info("%s: mensaje propio, ignorando.", prefijo)
            return
        if sender_id in EXCLUIR_BOT:
            log.info("%s: atendido por humano %s, ignorando.", prefijo, sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return
        if await usuario_pausado(sender_id):
            log.info("%s: bot pausado para %s, ignorando.", prefijo, sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return

        async with _user_locks[sender_id]:
            cerrada = await conversacion_cerrada(sender_id)
            if cerrada:
                await resetear_cerrada(sender_id)
                canonical = await obtener_canonical_id(sender_id)
                await limpiar_historial(canonical)
                log.info("%s: conversación cerrada reseteada para %s — procesando mensaje con Claude", prefijo, sender_id)

            log.info("%s user=%s: %s", prefijo, sender_id, mensaje[:100])
            async with httpx.AsyncClient() as client:
                from config import AUTO_RESPUESTA, SALUDO
                if AUTO_RESPUESTA:
                    canonical = await obtener_canonical_id(sender_id)
                    await guardar_mensaje(canonical, "user", mensaje)
                    if cerrada or await es_usuario_nuevo(sender_id):
                        enviado = await modulo.enviar_mensaje(client, sender_id, SALUDO)
                        if enviado:
                            await guardar_mensaje(canonical, "assistant", SALUDO)
                        if not cerrada:
                            await marcar_saludado(sender_id, canal.nombre)
                    return

                nuevo = await es_usuario_nuevo(sender_id)
                if nuevo:
                    await marcar_saludado(sender_id, canal.nombre)
                    if canal.es_telefono:
                        odoo_match = await buscar_cliente_odoo_por_telefono(sender_id)
                        if odoo_match and odoo_match.get("nombre"):
                            await guardar_datos_cliente(sender_id, nombre=odoo_match["nombre"],
                                                        email=odoo_match.get("email") or "")
                            log.info("%s: cliente Odoo identificado para %s (%s)", prefijo, sender_id, odoo_match["nombre"])
                respuesta = await generar_respuesta(sender_id, mensaje, canal.nombre, es_nuevo=(cerrada or nuevo))
                if respuesta:
                    await modulo.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("%s error procesando evento: %s", prefijo, e)


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
    await _procesar_mensaje_canal(data, _CANAL_INSTAGRAM)


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
    await _procesar_mensaje_canal(data, _CANAL_WHATSAPP)
