import logging

import httpx

from config import MODO_DEV, WA_ACCESS_TOKEN, WA_PHONE_ID

log = logging.getLogger(__name__)


def extraer_message_id(data: dict) -> str:
    entry = data.get("entry", [{}])[0]
    for change in entry.get("changes", []):
        for msg in change.get("value", {}).get("messages", []):
            mid = msg.get("id", "")
            if mid:
                return mid
    return ""


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


async def obtener_plantillas(client: httpx.AsyncClient) -> dict:
    """Consulta en Meta las plantillas de mensaje aprobadas para esta cuenta de WhatsApp Business."""
    if not WA_ACCESS_TOKEN or not WA_PHONE_ID:
        return {"error": "WA_ACCESS_TOKEN o WA_PHONE_ID no configurados"}
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}
    try:
        r = await client.get(
            f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}",
            params={"fields": "whatsapp_business_account"},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        waba_id = r.json().get("whatsapp_business_account", {}).get("id")
        if not waba_id:
            return {"error": "No se pudo resolver whatsapp_business_account", "detalle": r.json()}

        r2 = await client.get(
            f"https://graph.facebook.com/v19.0/{waba_id}/message_templates",
            params={"limit": 100},
            headers=headers,
            timeout=15,
        )
        r2.raise_for_status()
        return r2.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "detalle": e.response.text}
    except Exception as e:
        return {"error": str(e)}


async def enviar_plantilla(
    client: httpx.AsyncClient, recipient_id: str, nombre_plantilla: str,
    idioma: str, parametros: list[str],
) -> bool:
    """Envia un mensaje de plantilla aprobada por Meta (requerido fuera de la ventana de 24hs)."""
    if MODO_DEV:
        log.info("MODO_DEV activo - plantilla WA NO enviada (simulado) a %s: %s %s",
                  recipient_id, nombre_plantilla, parametros)
        return True

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
        "type": "template",
        "template": {
            "name": nombre_plantilla,
            "language": {"code": idioma},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in parametros],
            }],
        },
    }
    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            log.info("WA plantilla '%s' enviada a %s", nombre_plantilla, recipient_id)
            return True
        log.warning("WA plantilla error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("WA plantilla excepcion: %s", e)
    return False


async def enviar_mensaje(client: httpx.AsyncClient, recipient_id: str, texto: str) -> bool:
    if MODO_DEV:
        log.info("MODO_DEV activo — mensaje WA NO enviado (simulado) a %s: %s", recipient_id, texto[:80])
        return True

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
