"""
Instagram → Odoo Live Chat Agent Bridge
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
import time

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ─── CONFIG DESDE .ENV ───────────────────────────────────────────────────────
VERIFY_TOKEN       = os.environ["META_VERIFY_TOKEN"]
APP_SECRET         = os.environ["META_APP_SECRET"]
IG_ACCESS_TOKEN    = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID      = os.environ["IG_ACCOUNT_ID"]
ODOO_URL           = os.environ["ODOO_URL"].rstrip("/")
ODOO_API_KEY       = os.environ["ODOO_API_KEY"]
ODOO_CHANNEL_ID    = int(os.environ["ODOO_LIVECHAT_CHANNEL_ID"])
ODOO_DB            = os.environ["ODOO_DB"]

# ─── APP ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Instagram-Odoo Agent Bridge", version="1.0.0")

# Sesiones activas: { instagram_user_id: odoo_discuss_channel_id }
sesiones: dict[str, int] = {}


# ─── HELPERS ODOO ─────────────────────────────────────────────────────────────

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
            "params": {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"Odoo error: {data['error']}")
    return data["result"]


async def obtener_o_crear_sesion(client: httpx.AsyncClient, ig_user_id: str, nombre: str) -> int:
    """Retorna el discuss.channel id de la sesión activa, o crea una nueva."""
    if ig_user_id in sesiones:
        return sesiones[ig_user_id]

    # Crear nueva sesión en el canal Live Chat
    resp = await client.post(
        f"{ODOO_URL}/im_livechat/get_session",
        headers=odoo_headers(),
        json={
            "channel_id": ODOO_CHANNEL_ID,
            "anonymous_name": nombre or f"Instagram_{ig_user_id}",
            "previous_operator_id": False,
            "persisted": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    channel_id = data.get("result", {}).get("id") or data.get("id")

    if not channel_id:
        raise Exception(f"No se pudo crear sesión en Odoo. Respuesta: {data}")

    sesiones[ig_user_id] = channel_id
    log.info("Nueva sesión Odoo creada: channel_id=%s para IG user=%s", channel_id, ig_user_id)
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


async def esperar_respuesta_agente(client: httpx.AsyncClient, channel_id: int, espera: int = 8) -> str:
    """
    Espera hasta `espera` segundos y devuelve la última respuesta del agente.
    Hace polling cada 1 segundo.
    """
    ts_inicio = time.time()
    ultima_respuesta = ""

    while time.time() - ts_inicio < espera:
        await asyncio.sleep(1.5)

        mensajes = await odoo_rpc(
            client,
            model="discuss.channel",
            method="message_fetch",
            args=[[["res_id", "=", channel_id], ["model", "=", "discuss.channel"], ["author_id.name", "!=", "OdooBot"]]],
            kwargs={"limit": 5},
        )

        # Buscar el mensaje más reciente que NO sea del cliente (es decir, del agente)
        for msg in reversed(mensajes or []):
            autor = msg.get("author", {}).get("name", "")
            cuerpo = msg.get("body", "").strip()
            # Filtrar mensajes vacíos o del sistema
            if cuerpo and autor and "Instagram" not in autor:
                if cuerpo != ultima_respuesta:
                    ultima_respuesta = cuerpo
                    # Limpiar HTML básico
                    import re
                    ultima_respuesta = re.sub(r"<[^>]+>", "", ultima_respuesta).strip()
                    log.info("Respuesta del agente recibida: %s...", ultima_respuesta[:60])
                    return ultima_respuesta

    return ultima_respuesta or "Hola, en este momento no puedo responderte. Te contactamos a la brevedad."


# ─── HELPERS INSTAGRAM ────────────────────────────────────────────────────────

async def enviar_mensaje_instagram(client: httpx.AsyncClient, recipient_id: str, texto: str) -> None:
    """Envía un mensaje al DM de Instagram."""
    resp = await client.post(
        f"https://graph.instagram.com/v19.0/{IG_ACCOUNT_ID}/messages",
        params={"access_token": IG_ACCESS_TOKEN},
        json={
            "recipient": {"id": recipient_id},
            "message": {"text": texto},
            "messaging_type": "RESPONSE",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        log.error("Error enviando mensaje IG: %s", resp.text)
    else:
        log.info("Mensaje enviado a Instagram user=%s", recipient_id)


def verificar_firma(payload: bytes, firma_header: str) -> bool:
    """Verifica la firma HMAC-SHA256 de Meta para autenticar el webhook."""
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(firma_header[7:], firma_esperada)


# ─── MODO AUTO-RESPUESTA ──────────────────────────────────────────────────────
# Ponelo en True para verificar que el webhook funciona de punta a punta.
# Una vez confirmado, cambialo a False para activar la lógica con Odoo/AI.
AUTO_RESPUESTA = True


# ─── RUTAS ────────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    """Endpoint de verificación que Meta llama al configurar el webhook."""
    params = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado correctamente por Meta")
        return PlainTextResponse(challenge)

    log.warning("Verificación de webhook fallida")
    raise HTTPException(status_code=403, detail="Token de verificación incorrecto")


@app.post("/webhook")
async def recibir_mensaje(request: Request):
    """Recibe los eventos de mensajes de Instagram."""
    payload = await request.body()

    # Verificar firma de seguridad
    firma = request.headers.get("X-Hub-Signature-256", "")
    if not verificar_firma(payload, firma):
        log.warning("Firma inválida — posible request no autorizado")
        raise HTTPException(status_code=401, detail="Firma inválida")

    data = await request.json()
    log.info("Evento recibido: %s", str(data)[:200])

    # Procesar en background para responder 200 rápido a Meta
    asyncio.create_task(procesar_evento(data))

    return Response(status_code=200)


def extraer_sender_y_mensaje(data: dict) -> tuple[str, str]:
    """Extrae sender_id y texto del payload de Meta (Instagram DM)."""
    entry = data.get("entry", [{}])[0]

    # Formato estándar de Instagram Messaging
    messaging_list = entry.get("messaging") or []
    if messaging_list:
        messaging = messaging_list[0]
        sender_id = messaging.get("sender", {}).get("id", "")
        mensaje   = messaging.get("message", {}).get("text", "")
        return sender_id, mensaje

    # Formato alternativo via "changes" (WhatsApp / algunas cuentas IG Business)
    for change in entry.get("changes", []):
        value = change.get("value", {})
        messages = value.get("messages", [])
        if messages:
            msg = messages[0]
            sender_id = (
                msg.get("from", {}).get("id")
                or value.get("contacts", [{}])[0].get("wa_id", "")
            )
            mensaje = msg.get("text", {}).get("body", "")
            return sender_id, mensaje

    return "", ""


async def procesar_evento(data: dict):
    """Procesa el evento de Instagram de forma asíncrona."""
    try:
        log.info("PAYLOAD COMPLETO: %s", str(data))

        sender_id, mensaje = extraer_sender_y_mensaje(data)

        if not sender_id or not mensaje:
            log.info("Evento sin sender_id o mensaje, ignorando.")
            return

        log.info("Mensaje recibido de IG user=%s: %s", sender_id, mensaje[:80])

        async with httpx.AsyncClient() as client:
            if AUTO_RESPUESTA:
                # Modo verificación: eco simple sin Odoo ni AI
                respuesta = f"Bot activo - recibi tu mensaje: {mensaje}"
                await enviar_mensaje_instagram(client, sender_id, respuesta)
                log.info("Auto-respuesta enviada a IG user=%s", sender_id)
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
    """Health check para Railway."""
    return {"status": "ok", "servicio": "Instagram-Odoo Agent Bridge"}
