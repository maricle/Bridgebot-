"""
BridgeBot — Instagram → Odoo Live Chat Agent
Kleba Dev — 2026

Recibe mensajes de Instagram via webhook de Meta,
los envía al agente de Odoo Live Chat y devuelve
la respuesta al DM del cliente.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
VERIFY_TOKEN          = os.environ["META_VERIFY_TOKEN"]
APP_SECRET            = os.environ["META_APP_SECRET"]
IG_ACCESS_TOKEN       = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID         = os.environ.get("IG_ACCOUNT_ID", "17841456843060136")
ODOO_URL              = os.environ["ODOO_URL"].rstrip("/")
ODOO_API_KEY          = os.environ["ODOO_API_KEY"]
ODOO_CHANNEL_ID       = int(os.environ["ODOO_LIVECHAT_CHANNEL_ID"])
ODOO_DB               = os.environ["ODOO_DB"]

# ─── MODO PRUEBA ─────────────────────────────────────────────────────────────
# True  → responde un eco simple para verificar que el token de IG funciona
# False → activa el flujo completo con Odoo AI
AUTO_RESPUESTA = os.environ.get("AUTO_RESPUESTA", "true").lower() == "true"

# ─── APP ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="BridgeBot — Instagram Odoo Agent", version="2.0.0")

# Sesiones activas: { instagram_user_id: odoo_discuss_channel_id }
sesiones: dict[str, int] = {}


# ─── ODOO ─────────────────────────────────────────────────────────────────────

def odoo_headers() -> dict:
    return {
        "Authorization": f"Bearer {ODOO_API_KEY}",
        "Content-Type": "application/json",
        "X-Odoo-Database": ODOO_DB,
    }


async def odoo_rpc(client: httpx.AsyncClient, model: str, method: str, args: list, kwargs: dict = {}) -> any:
    """Llama al endpoint JSON-RPC de Odoo."""
    resp = await client.post(
        f"{ODOO_URL}/web/dataset/call_kw",
        headers=odoo_headers(),
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"Odoo RPC error: {data['error']}")
    return data["result"]


async def obtener_o_crear_sesion(client: httpx.AsyncClient, ig_user_id: str, nombre: str) -> int:
    """Retorna el discuss.channel id activo o crea uno nuevo. Compatible con Odoo 19."""
    if ig_user_id in sesiones:
        return sesiones[ig_user_id]

    nombre_visitante = nombre or f"Instagram_{ig_user_id}"
    channel_id = None

    # Intento 1 — método nativo de livechat
    try:
        result = await odoo_rpc(
            client,
            model="im_livechat.channel",
            method="_open_livechat_mail_channel",
            args=[ODOO_CHANNEL_ID],
            kwargs={
                "anonymous_name": nombre_visitante,
                "previous_operator_id": False,
                "chatbot_script_id": False,
                "persisted": True,
            },
        )
        if isinstance(result, dict):
            channel_id = result.get("id") or result.get("discuss_channel_id", {}).get("id")
        else:
            channel_id = result
        log.info("Sesión creada via _open_livechat_mail_channel: %s", channel_id)
    except Exception as e:
        log.warning("_open_livechat_mail_channel falló: %s", e)

    # Intento 2 — crear canal discuss directamente
    if not channel_id:
        try:
            channel_id = await odoo_rpc(
                client,
                model="discuss.channel",
                method="create",
                args=[{
                    "name": f"IG - {nombre_visitante}",
                    "channel_type": "livechat",
                    "livechat_channel_id": ODOO_CHANNEL_ID,
                }],
            )
            log.info("Sesión creada via discuss.channel create: %s", channel_id)
        except Exception as e:
            log.warning("discuss.channel create falló: %s", e)

    if not channel_id:
        raise Exception("No se pudo crear sesión en Odoo — revisá los permisos de la API key")

    sesiones[ig_user_id] = channel_id
    log.info("Sesion Odoo lista: channel_id=%s para IG user=%s", channel_id, ig_user_id)
    return channel_id


async def enviar_mensaje_odoo(client: httpx.AsyncClient, channel_id: int, texto: str) -> None:
    """Envía el mensaje del cliente al canal de Odoo."""
    await odoo_rpc(
        client,
        model="discuss.channel",
        method="message_post",
        args=[channel_id],
        kwargs={
            "body": texto,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
        },
    )
    log.info("Mensaje enviado a Odoo channel=%s", channel_id)


async def esperar_respuesta_agente(client: httpx.AsyncClient, channel_id: int, espera: int = 10) -> str:
    """Hace polling hasta `espera` segundos esperando la respuesta del agente."""
    ts_inicio = time.time()
    ultima_respuesta = ""

    while time.time() - ts_inicio < espera:
        await asyncio.sleep(2)
        try:
            mensajes = await odoo_rpc(
                client,
                model="discuss.channel",
                method="message_fetch",
                args=[[
                    ["res_id", "=", channel_id],
                    ["model", "=", "discuss.channel"],
                ]],
                kwargs={"limit": 10},
            )
            for msg in reversed(mensajes or []):
                autor = msg.get("author", {}).get("name", "")
                cuerpo = msg.get("body", "").strip()
                if cuerpo and autor and "Instagram" not in autor and "OdooBot" not in autor:
                    texto_limpio = re.sub(r"<[^>]+>", "", cuerpo).strip()
                    if texto_limpio and texto_limpio != ultima_respuesta:
                        ultima_respuesta = texto_limpio
                        log.info("Respuesta del agente: %s...", ultima_respuesta[:80])
                        return ultima_respuesta
        except Exception as e:
            log.warning("Error leyendo mensajes de Odoo: %s", e)

    return ultima_respuesta or "Hola, gracias por tu mensaje. Te respondemos a la brevedad."


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

async def enviar_mensaje_instagram(client: httpx.AsyncClient, recipient_id: str, texto: str) -> None:
    """Envía respuesta al DM de Instagram. Intenta graph.instagram.com y graph.facebook.com como fallback."""
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": texto},
        "messaging_type": "RESPONSE",
    }
    headers = {
        "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    endpoints = [
        f"https://graph.instagram.com/v19.0/{IG_ACCOUNT_ID}/messages",
        f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/messages",
    ]

    for url in endpoints:
        resp = await client.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            log.info("DM enviado a IG user=%s via %s", recipient_id, url.split("/")[2])
            return
        log.warning("Fallo %s: %s %s", url.split("/")[2], resp.status_code, resp.text)

    log.error("No se pudo enviar el DM a Instagram user=%s", recipient_id)


# ─── SEGURIDAD ────────────────────────────────────────────────────────────────

def verificar_firma(payload: bytes, firma_header: str) -> bool:
    """Verifica la firma HMAC-SHA256 de Meta."""
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma_header[7:], firma_esperada)


def extraer_sender_y_mensaje(data: dict) -> tuple[str, str]:
    """Extrae sender_id y texto del payload de Meta."""
    entry = data.get("entry", [{}])[0]

    for msg in entry.get("messaging", []):
        sender_id = msg.get("sender", {}).get("id", "")
        texto     = msg.get("message", {}).get("text", "")
        if sender_id and texto:
            return sender_id, texto

    for change in entry.get("changes", []):
        value    = change.get("value", {})
        messages = value.get("messages", [])
        if messages:
            msg       = messages[0]
            sender_id = msg.get("from", {}).get("id") or value.get("contacts", [{}])[0].get("wa_id", "")
            texto     = msg.get("text", {}).get("body", "")
            if sender_id and texto:
                return sender_id, texto

    return "", ""


# ─── RUTAS ────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    """Verificación del webhook por Meta."""
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token de verificación incorrecto")


@app.post("/webhook")
async def recibir_mensaje(request: Request):
    """Recibe eventos de Instagram."""
    payload = await request.body()
    firma   = request.headers.get("X-Hub-Signature-256", "")

    if not verificar_firma(payload, firma):
        log.warning("Firma invalida")
        raise HTTPException(status_code=401, detail="Firma inválida")

    data = await request.json()
    log.info("Evento recibido: %s", str(data)[:300])
    asyncio.create_task(procesar_evento(data))
    return Response(status_code=200)


async def procesar_evento(data: dict):
    """Procesa el evento de forma asíncrona."""
    try:
        sender_id, mensaje = extraer_sender_y_mensaje(data)

        if not sender_id or not mensaje:
            log.info("Evento sin mensaje de texto, ignorando.")
            return

        log.info("Mensaje de IG user=%s: %s", sender_id, mensaje[:100])

        async with httpx.AsyncClient() as client:

            if AUTO_RESPUESTA:
                respuesta = f"BridgeBot activo — recibi: {mensaje}"
                await enviar_mensaje_instagram(client, sender_id, respuesta)
                log.info("Modo AUTO_RESPUESTA — eco enviado a %s", sender_id)
                return

            # ── Modo completo: Odoo + AI ──────────────────────────────────────
            channel_id = await obtener_o_crear_sesion(client, sender_id, f"IG_{sender_id}")
            await enviar_mensaje_odoo(client, channel_id, mensaje)
            respuesta = await esperar_respuesta_agente(client, channel_id)
            await enviar_mensaje_instagram(client, sender_id, respuesta)

    except Exception as e:
        log.exception("Error procesando evento: %s", e)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "servicio": "BridgeBot",
        "modo": "AUTO_RESPUESTA" if AUTO_RESPUESTA else "ODOO_AI",
    }
