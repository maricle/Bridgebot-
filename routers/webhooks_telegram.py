"""Bot interno de Telegram — uso exclusivo del equipo, no atiende clientes.
Comando /tarea <descripción> crea un project.task personal en Odoo."""

import logging

import httpx
from fastapi import APIRouter, Request

import odoo_crm
import telegram_bot
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_WEBHOOK_SECRET

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook/telegram")
async def recibir_telegram(request: Request):
    """Siempre responde 200 — Telegram reintenta el update si no recibe 200,
    y un update irrelevante (no es /tarea) o de un chat no autorizado no debe
    generar reintentos."""
    if TELEGRAM_WEBHOOK_SECRET:
        secreto = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secreto != TELEGRAM_WEBHOOK_SECRET:
            log.warning("Telegram: webhook con secreto inválido, ignorado")
            return {"ok": True}

    data = await request.json()
    chat_id, descripcion = telegram_bot.extraer_comando_tarea(data)
    if not chat_id:
        return {"ok": True}

    async with httpx.AsyncClient() as client:
        if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
            log.warning("Telegram: chat no autorizado (%s), ignorado", chat_id)
            await telegram_bot.enviar_mensaje(client, chat_id, "No autorizado.")
            return {"ok": True}

        if not descripcion:
            await telegram_bot.enviar_mensaje(client, chat_id, "Uso: /tarea <descripción>")
            return {"ok": True}

        task_id = await odoo_crm.crear_tarea_personal(descripcion)
        if task_id:
            await telegram_bot.enviar_mensaje(client, chat_id, f"✅ Tarea creada (#{task_id}): {descripcion}")
        else:
            await telegram_bot.enviar_mensaje(client, chat_id, "⚠️ No se pudo crear la tarea en Odoo — revisá los logs.")

    return {"ok": True}


@router.get("/telegram/set-webhook")
async def telegram_set_webhook(url: str):
    """Registra `url` (ej. https://tu-app.railway.app/webhook/telegram) como
    webhook del bot — se llama una sola vez al configurar el bot o si cambia
    el dominio de deploy."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN no configurado"}
    params = {"url": url}
    if TELEGRAM_WEBHOOK_SECRET:
        params["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            params=params, timeout=15,
        )
        return resp.json()
