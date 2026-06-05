import logging

import httpx

from config import WA_ACCESS_TOKEN, WA_PHONE_ID

log = logging.getLogger(__name__)


def extraer_mensaje(data: dict) -> tuple[str, str]:
    """Retorna (sender_id, texto) del payload de WhatsApp Business."""
    entry = data.get("entry", [{}])[0]
    for change in entry.get("changes", []):
        value = change.get("value", {})
        for msg in value.get("messages", []):
            sender_id = msg.get("from", "")
            if msg.get("type") == "text":
                texto = msg.get("text", {}).get("body", "")
                if sender_id and texto:
                    return sender_id, texto
    return "", ""


_TIPOS_MEDIA = ("image", "document", "video", "audio", "sticker")


def extraer_archivos(data: dict) -> tuple[str, list[dict]]:
    """Retorna (sender_id, lista de archivos) del payload de WhatsApp."""
    entry = data.get("entry", [{}])[0]
    for change in entry.get("changes", []):
        for msg in change.get("value", {}).get("messages", []):
            sender_id = msg.get("from", "")
            tipo = msg.get("type", "")
            if sender_id and tipo in _TIPOS_MEDIA:
                media = msg.get(tipo, {})
                return sender_id, [{
                    "tipo": tipo,
                    "media_id": media.get("id", ""),
                    "mime_type": media.get("mime_type", ""),
                }]
    return "", []


async def enviar_mensaje(client: httpx.AsyncClient, recipient_id: str, texto: str) -> bool:
    if not WA_ACCESS_TOKEN or not WA_PHONE_ID:
        log.error("WA_ACCESS_TOKEN o WA_PHONE_ID no configurados")
        return False

    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            log.info("WA mensaje enviado a %s", recipient_id)
            return True
        log.warning("WA error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("WA excepción: %s", e)
    return False
