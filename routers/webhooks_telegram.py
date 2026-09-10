"""Bot interno de Telegram — uso exclusivo del equipo, no atiende clientes.

Flujo de alta (una sola vez por chat):
  1. /start → el bot pide compartir el teléfono (botón nativo de Telegram,
     no se puede tipear cualquier cosa — es el teléfono real de esa cuenta).
  2. El usuario comparte el teléfono → el bot pide el correo de Odoo.
  3. El usuario escribe el correo → se valida contra Odoo (odoo_crm.validar_usuario:
     el correo tiene que ser un res.users real y su teléfono/móvil en Odoo
     tiene que coincidir con el compartido). Si valida, el chat queda
     registrado (db.registrar_telegram_usuario) y ya puede usar /tarea.

El teléfono/correo solo autorizan QUIÉN puede usar el bot — no determinan a
quién se le asigna la tarea: /tarea siempre crea el project.task con el
usuario fijo de TELEGRAM_ODOO_USER_ID (ver odoo_crm.crear_tarea_personal)."""

import logging

import httpx
from fastapi import APIRouter, Request

import db
import odoo_crm
import telegram_bot
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET

log = logging.getLogger(__name__)
router = APIRouter()

# chat_id -> teléfono compartido, a la espera del correo que confirma el
# registro. Estado en memoria: si el proceso se reinicia a mitad del alta,
# el usuario vuelve a compartir el teléfono — no amerita persistirlo en DB.
_registros_pendientes: dict[str, str] = {}


@router.post("/webhook/telegram")
async def recibir_telegram(request: Request):
    """Siempre responde 200 — Telegram reintenta el update si no recibe 200 a
    tiempo, y un update irrelevante o de un chat no registrado no debe
    generar reintentos."""
    if TELEGRAM_WEBHOOK_SECRET:
        secreto = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secreto != TELEGRAM_WEBHOOK_SECRET:
            log.warning("Telegram: webhook con secreto inválido, ignorado")
            return {"ok": True}

    data = await request.json()

    chat_id, telefono = telegram_bot.extraer_contacto(data)
    if chat_id:
        await _procesar_contacto(chat_id, telefono)
        return {"ok": True}

    chat_id, texto = telegram_bot.extraer_texto(data)
    if not chat_id:
        return {"ok": True}

    if texto == "/start":
        await _procesar_start(chat_id)
        return {"ok": True}

    descripcion = telegram_bot.parsear_comando_tarea(texto)
    if descripcion is not None:
        await _procesar_tarea(chat_id, descripcion)
        return {"ok": True}

    if chat_id in _registros_pendientes:
        await _procesar_correo(chat_id, texto)
        return {"ok": True}

    if not await db.telegram_usuario_registrado(chat_id):
        async with httpx.AsyncClient() as client:
            await telegram_bot.enviar_mensaje(client, chat_id, "No entendí ese mensaje. Escribí /start para registrarte.")

    return {"ok": True}


async def _procesar_contacto(chat_id: str, telefono: str):
    _registros_pendientes[chat_id] = telefono
    async with httpx.AsyncClient() as client:
        await telegram_bot.enviar_mensaje(
            client, chat_id,
            "Gracias. Ahora enviame tu correo de Odoo (el que usás para iniciar sesión) para validar tu usuario.",
            teclado=telegram_bot.teclado_ocultar(),
        )


async def _procesar_start(chat_id: str):
    async with httpx.AsyncClient() as client:
        if await db.telegram_usuario_registrado(chat_id):
            await telegram_bot.enviar_mensaje(client, chat_id, "Ya estás registrado. Usá /tarea <descripción> para crear una tarea.")
            return
        await telegram_bot.enviar_mensaje(
            client, chat_id,
            "Para usar este bot necesito validar tu usuario de Odoo. Compartí tu teléfono con el botón de abajo.",
            teclado=telegram_bot.teclado_compartir_telefono(),
        )


async def _procesar_correo(chat_id: str, texto: str):
    telefono = _registros_pendientes.pop(chat_id, "")
    email = texto.strip()
    resultado = await odoo_crm.validar_usuario(email, telefono)
    async with httpx.AsyncClient() as client:
        if not resultado:
            await telegram_bot.enviar_mensaje(
                client, chat_id,
                "No pude validar ese correo con tu teléfono en Odoo. Revisá los datos y escribí /start para reintentar.",
            )
            return
        await db.registrar_telegram_usuario(
            chat_id, telefono, email, resultado["nombre"], resultado["odoo_user_id"],
        )
        await telegram_bot.enviar_mensaje(
            client, chat_id,
            f"✅ Usuario validado: {resultado['nombre']}. Ya podés usar /tarea <descripción>.",
        )


async def _procesar_tarea(chat_id: str, descripcion: str):
    async with httpx.AsyncClient() as client:
        if not await db.telegram_usuario_registrado(chat_id):
            await telegram_bot.enviar_mensaje(client, chat_id, "Todavía no registraste tu usuario. Escribí /start para empezar.")
            return
        if not descripcion:
            await telegram_bot.enviar_mensaje(client, chat_id, "Uso: /tarea <descripción>")
            return

        usuario = await db.obtener_telegram_usuario(chat_id)
        solicitante = usuario["nombre"] if usuario else ""
        task_id = await odoo_crm.crear_tarea_personal(descripcion, solicitante=solicitante)
        if task_id:
            await telegram_bot.enviar_mensaje(client, chat_id, f"✅ Tarea creada (#{task_id}): {descripcion}")
        else:
            await telegram_bot.enviar_mensaje(client, chat_id, "⚠️ No se pudo crear la tarea en Odoo — revisá los logs.")


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
