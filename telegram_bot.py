import logging
import re

import httpx

from config import TELEGRAM_BOT_TOKEN

log = logging.getLogger(__name__)

_COMANDO_TAREA_RE = re.compile(r'^/tarea(?:@\w+)?(?:\s+(.+))?$', re.DOTALL)


def extraer_texto(data: dict) -> tuple[str, str]:
    """Retorna (chat_id, texto) de cualquier mensaje de texto. ("", "") para
    updates sin texto (contacto compartido, ediciones, stickers, etc.)."""
    mensaje = data.get("message") or {}
    texto = mensaje.get("text", "")
    chat_id = str(mensaje.get("chat", {}).get("id", "") or "")
    if chat_id and texto:
        return chat_id, texto.strip()
    return "", ""


def extraer_contacto(data: dict) -> tuple[str, str]:
    """Retorna (chat_id, telefono) si el update trae un contacto compartido
    con el botón nativo de Telegram (teclado_compartir_telefono) Y pertenece
    al mismo usuario que lo envía — si alguien reenvía la tarjeta de contacto
    de otra persona, contact.user_id no coincide con from.id y no vale para
    autorizarse con un teléfono ajeno. ("", "") si no aplica."""
    mensaje = data.get("message") or {}
    contacto = mensaje.get("contact")
    if not contacto:
        return "", ""
    remitente_id = mensaje.get("from", {}).get("id")
    contacto_id = contacto.get("user_id")
    if contacto_id and remitente_id and contacto_id != remitente_id:
        return "", ""
    chat_id = str(mensaje.get("chat", {}).get("id", "") or "")
    telefono = contacto.get("phone_number", "")
    if chat_id and telefono:
        return chat_id, telefono
    return "", ""


def parsear_comando_tarea(texto: str) -> str | None:
    """None si `texto` no es el comando /tarea. Si lo es, retorna la
    descripción (puede ser "" si vino sin texto — el caller responde el uso)."""
    m = _COMANDO_TAREA_RE.match(texto.strip())
    return (m.group(1) or "").strip() if m else None


def extraer_comando_tarea(data: dict) -> tuple[str, str]:
    """Atajo de extraer_texto + parsear_comando_tarea para el caso simple.
    Retorna (chat_id, descripcion), o ("", "") si el update no es /tarea."""
    chat_id, texto = extraer_texto(data)
    if not chat_id:
        return "", ""
    descripcion = parsear_comando_tarea(texto)
    return (chat_id, descripcion) if descripcion is not None else ("", "")


def teclado_compartir_telefono() -> dict:
    """reply_markup con un botón que comparte el teléfono real de la cuenta
    de Telegram — Telegram lo completa solo, no se puede tipear cualquier cosa."""
    return {
        "keyboard": [[{"text": "📱 Compartir mi teléfono", "request_contact": True}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }


def teclado_ocultar() -> dict:
    return {"remove_keyboard": True}


async def enviar_mensaje(client: httpx.AsyncClient, chat_id: str, texto: str, teclado: dict | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN no configurado")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": texto}
    if teclado is not None:
        payload["reply_markup"] = teclado
    try:
        resp = await client.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        log.warning("Telegram sendMessage error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram sendMessage excepción: %s", e)
    return False
