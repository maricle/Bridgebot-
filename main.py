"""
BridgeBot — Instagram + WhatsApp → Groq AI Agent
Kleba Dev — 2026
"""

import asyncio
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

import instagram
import whatsapp
from config import AUTO_RESPUESTA, IG_ACCOUNT_ID, SALUDO, VERIFY_TOKEN
from db import (es_usuario_nuevo, init_db, marcar_saludado, obtener_conversacion,
                obtener_leads, obtener_usuarios, resetear_usuario, stats)
from groq_ai import generar_respuesta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(title="BridgeBot", version="5.0.0")


# ─── STARTUP ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()
    modo = "AUTO_RESPUESTA" if AUTO_RESPUESTA else "GROQ_AI"
    log.info("BridgeBot v5 iniciado — modo: %s", modo)
    log.info("GROQ configurado: %s", "SI" if __import__("config").GROQ_API_KEY else "NO")


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
        sender_id, mensaje = instagram.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("IG: evento sin texto, ignorando.")
            return
        if sender_id == IG_ACCOUNT_ID:
            log.info("IG: mensaje propio, ignorando.")
            return

        log.info("IG user=%s: %s", sender_id, mensaje[:100])
        async with httpx.AsyncClient() as client:
            if AUTO_RESPUESTA:
                if await es_usuario_nuevo(sender_id):
                    await instagram.enviar_mensaje(client, sender_id, SALUDO)
                    await marcar_saludado(sender_id, "instagram")
                return

            nuevo     = await es_usuario_nuevo(sender_id)
            if nuevo:
                await marcar_saludado(sender_id, "instagram")
                await instagram.enviar_mensaje(client, sender_id, SALUDO)
            respuesta = await generar_respuesta(sender_id, mensaje, "instagram")
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
    payload = await request.body()
    firma   = request.headers.get("X-Hub-Signature-256", "")
    if firma and not instagram.verificar_firma(payload, firma):
        log.warning("WA firma inválida, ignorando verificación en modo debug")
    data = await request.json()
    log.info("WA webhook recibido: %s", str(data)[:200])
    asyncio.create_task(procesar_whatsapp(data))
    return Response(status_code=200)


async def procesar_whatsapp(data: dict):
    try:
        sender_id, mensaje = whatsapp.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("WA: evento sin texto, ignorando.")
            return

        log.info("WA user=%s: %s", sender_id, mensaje[:100])
        async with httpx.AsyncClient() as client:
            nuevo     = await es_usuario_nuevo(sender_id)
            if nuevo:
                await marcar_saludado(sender_id, "whatsapp")
                await whatsapp.enviar_mensaje(client, sender_id, SALUDO)
            respuesta = await generar_respuesta(sender_id, mensaje, "whatsapp")
            await whatsapp.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("WA error procesando evento: %s", e)


# ─── UTILS ────────────────────────────────────────────────────────────────────

@app.get("/test-odoo")
async def test_odoo():
    from odoo_crm import crear_lead
    lead_id = await crear_lead(
        nombre_cliente="Test BridgeBot",
        telefono="0000000000",
        descripcion="Lead de prueba desde /test-odoo — podés eliminarlo.",
        canal="test",
        user_id="test",
    )
    if lead_id:
        return {"ok": True, "odoo_lead_id": lead_id, "mensaje": "Lead creado correctamente en Odoo CRM"}
    return {"ok": False, "mensaje": "No se pudo crear el lead — revisá ODOO_URL, ODOO_API_KEY y ODOO_DB en Railway"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "5.0.0",
        "modo": "AUTO_RESPUESTA" if AUTO_RESPUESTA else "GROQ_AI",
        **(await stats()),
    }


@app.get("/leads")
async def ver_leads():
    return await obtener_leads()


@app.get("/usuarios")
async def ver_usuarios():
    return await obtener_usuarios()


@app.get("/conversacion/{user_id}")
async def ver_conversacion(user_id: str):
    return await obtener_conversacion(user_id)


@app.delete("/usuario/{user_id}")
async def borrar_usuario(user_id: str):
    await resetear_usuario(user_id)
    return {"ok": True, "mensaje": f"Usuario {user_id} reseteado"}
