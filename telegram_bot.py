import logging
import re

import httpx

from config import TELEGRAM_BOT_TOKEN

log = logging.getLogger(__name__)

_COMANDO_TAREA_RE = re.compile(r'^/tarea(?:@\w+)?(?:\s+(.+))?$', re.DOTALL)


def extraer_comando_tarea(data: dict) -> tuple[str, str]:
    """Retorna (chat_id, descripcion) si el update de Telegram es un mensaje
    de texto "/tarea" o "/tarea <descripcion>" — descripcion vacía si vino sin
    texto (el caller responde el uso correcto). ("", "") para cualquier otro
    update (Telegram manda updates de muchos tipos — ediciones, stickers,
    otros comandos — que hay que ignorar sin romper)."""
    mensaje = data.get("message") or {}
    texto = mensaje.get("text", "")
    chat_id = str(mensaje.get("chat", {}).get("id", "") or "")
    m = _COMANDO_TAREA_RE.match(texto.strip())
    if chat_id and m:
        return chat_id, (m.group(1) or "").strip()
    return "", ""


async def enviar_mensaje(client: httpx.AsyncClient, chat_id: str, texto: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN no configurado")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = await client.post(url, json={"chat_id": chat_id, "text": texto}, timeout=15)
        if resp.status_code == 200:
            return True
        log.warning("Telegram sendMessage error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram sendMessage excepción: %s", e)
    return False
