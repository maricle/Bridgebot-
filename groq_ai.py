import json
import logging

import httpx

from config import ANTHROPIC_API_KEY, get_system_prompt
from db import guardar_lead, guardar_mensaje, obtener_historial, tiene_lead_activo

log = logging.getLogger(__name__)

EXTRACCION_PROMPT = """
Analizá esta conversación y extraé los datos del cliente.
Respondé SOLO con un JSON válido con este formato exacto (sin explicaciones):
{
  "tiene_lead": true/false,
  "nombre": "nombre y apellido completo del cliente o null",
  "telefono": "teléfono o WhatsApp del cliente o null",
  "descripcion": "resumen breve del pedido en 1-2 oraciones o null"
}

"tiene_lead" debe ser true SOLO si se cumplen LAS TRES condiciones:
1. El cliente proporcionó su nombre y apellido
2. El cliente proporcionó su teléfono o WhatsApp
3. El cliente expresó un pedido o consulta concreta
"""


async def _llamar_claude(messages: list, system: str = "", max_tokens: int = 400) -> str | None:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": messages,
                },
                timeout=25,
            )
            if resp.status_code != 200:
                log.error("Claude error %s: %s", resp.status_code, resp.text)
                return None
            return resp.json()["content"][0]["text"].strip()
    except httpx.TimeoutException:
        log.error("Claude timeout")
        return None
    except Exception as e:
        log.error("Claude excepción: %s", e)
        return None


async def _intentar_crear_lead(user_id: str, canal: str, historial: list):
    if await tiene_lead_activo(user_id):
        return

    conversacion = "\n".join(
        f"{'Cliente' if m['role'] == 'user' else 'Bot'}: {m['content']}"
        for m in historial[-10:]
    )

    resultado = await _llamar_claude(
        messages=[{"role": "user", "content": conversacion}],
        system=EXTRACCION_PROMPT,
        max_tokens=200,
    )

    if not resultado:
        return

    try:
        datos = json.loads(resultado)
    except json.JSONDecodeError:
        log.warning("Claude no devolvió JSON válido para extracción: %s", resultado[:100])
        return

    if not datos.get("tiene_lead"):
        return

    nombre      = datos.get("nombre") or ""
    telefono    = datos.get("telefono") or ""
    descripcion = datos.get("descripcion") or ""

    from odoo_crm import crear_lead
    odoo_id = await crear_lead(nombre, telefono, descripcion, canal, user_id) or 0
    await guardar_lead(user_id, descripcion, canal, odoo_id)
    log.info("Lead procesado — user=%s odoo_id=%s", user_id, odoo_id)


async def generar_respuesta(user_id: str, mensaje: str, canal: str = "instagram") -> str:
    if not ANTHROPIC_API_KEY:
        return "El servicio de IA no está configurado. Te contactamos a la brevedad."

    historial = await obtener_historial(user_id)
    messages  = historial + [{"role": "user", "content": mensaje}]

    respuesta = await _llamar_claude(
        messages=messages,
        system=get_system_prompt(),
    )

    if not respuesta:
        return "Tardamos un poco más de lo normal. ¿Podés repetir tu consulta?"

    await guardar_mensaje(user_id, "user", mensaje)
    await guardar_mensaje(user_id, "assistant", respuesta)

    turnos_cliente = sum(1 for m in messages if m["role"] == "user")
    if turnos_cliente >= 3 and turnos_cliente % 3 == 0:
        import asyncio
        asyncio.create_task(_intentar_crear_lead(user_id, canal, messages))

    log.info("Claude [%s] user=%s: %s...", canal, user_id, respuesta[:80])
    return respuesta
