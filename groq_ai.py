import asyncio
import json
import logging

import httpx

from config import ANTHROPIC_API_KEY, get_system_prompt

_PALABRAS_PRECIO = {
    # Consultas de precio explícitas
    "precio", "precios", "presupuesto", "costo", "costos",
    "cuanto", "cuánto", "vale", "sale", "tarifa", "valor",
    "cotizacion", "cotización", "plata", "pesos", "cobran", "cobras",
    # Productos con precio conocido — cargar lista proactivamente
    "placa", "ranurada", "laqueado", "laqueo", "lacar", "barniz",
    "corte cnc", "mecanizado", "ranurado",
}

def _pide_precio(mensaje: str) -> bool:
    texto = mensaje.lower()
    return any(p in texto for p in _PALABRAS_PRECIO)
from db import (cerrar_conversacion, guardar_datos_cliente, guardar_lead,
                guardar_mensaje, obtener_datos_cliente, obtener_historial,
                tiene_lead_activo)

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
1. Se conoce el nombre y apellido del cliente (puede venir de los datos conocidos al inicio)
2. Se conoce el teléfono o WhatsApp del cliente (puede venir de los datos conocidos al inicio)
3. El cliente tiene un pedido o consulta concreta (producto o proyecto definido)

Si hay datos conocidos marcados con [Nombre conocido] o [Teléfono conocido], usarlos directamente sin requerir que el cliente los repita.
"""


async def _llamar_claude(messages: list, system: str = "", max_tokens: int = 400) -> str | None:
    for intento in range(3):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "anthropic-beta": "prompt-caching-2024-07-31",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": max_tokens,
                        **({"system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]} if system else {}),
                        "messages": messages,
                    },
                    timeout=25,
                )
            if resp.status_code == 200:
                return resp.json()["content"][0]["text"].strip()
            if resp.status_code == 429:
                espera = 2 ** intento
                log.warning("Claude rate limit — reintentando en %ss (intento %s/3)", espera, intento + 1)
                await asyncio.sleep(espera)
                continue
            log.error("Claude error %s: %s", resp.status_code, resp.text[:200])
            return None
        except httpx.TimeoutException:
            log.error("Claude timeout (intento %s/3)", intento + 1)
        except Exception as e:
            log.error("Claude excepción: %s", e)
            return None
    return None


async def _intentar_crear_lead(user_id: str, canal: str, historial: list):
    datos_cliente = await obtener_datos_cliente(user_id)

    # Para WA el user_id ES el teléfono — guardarlo si aún no está
    if canal == "whatsapp" and not datos_cliente.get("telefono"):
        await guardar_datos_cliente(user_id, telefono=user_id)
        datos_cliente["telefono"] = user_id

    # Inyectar datos conocidos al inicio del contexto de extracción
    prefijo = ""
    if datos_cliente.get("nombre"):
        prefijo += f"[Nombre conocido del cliente: {datos_cliente['nombre']}]\n"
    if datos_cliente.get("telefono"):
        prefijo += f"[Teléfono conocido del cliente: {datos_cliente['telefono']}]\n"

    conversacion = prefijo + "\n".join(
        f"{'Cliente' if m['role'] == 'user' else 'Bot'}: {m['content']}"
        for m in historial[-10:]
    )

    resultado = await _llamar_claude(
        messages=[{"role": "user", "content": conversacion}],
        system=EXTRACCION_PROMPT,
        max_tokens=350,
    )

    if not resultado:
        return

    try:
        # Claude a veces envuelve el JSON en ```json ... ```
        limpio = resultado.strip()
        if limpio.startswith("```"):
            limpio = limpio.split("\n", 1)[-1]
            limpio = limpio.rsplit("```", 1)[0]
        datos = json.loads(limpio.strip())
    except json.JSONDecodeError:
        log.warning("Claude no devolvió JSON válido para extracción: %s", resultado[:100])
        return

    if not datos.get("tiene_lead"):
        return

    if await tiene_lead_activo(user_id):
        return

    nombre      = datos.get("nombre") or ""
    telefono    = datos.get("telefono") or ""
    descripcion = datos.get("descripcion") or ""

    # En WhatsApp el user_id ES el número de teléfono
    if canal == "whatsapp" and not telefono:
        telefono = user_id

    from odoo_crm import crear_lead
    odoo_id = await crear_lead(nombre, telefono, descripcion, canal, user_id) or 0
    await guardar_lead(user_id, descripcion, canal, odoo_id)
    await guardar_datos_cliente(user_id, nombre=nombre, telefono=telefono)
    await cerrar_conversacion(user_id)
    log.info("Lead creado y conversación cerrada — user=%s odoo_id=%s", user_id, odoo_id)


async def generar_respuesta(user_id: str, mensaje: str, canal: str = "instagram") -> str:
    if not ANTHROPIC_API_KEY:
        return "El servicio de IA no está configurado. Te contactamos a la brevedad."

    historial        = await obtener_historial(user_id)
    datos_cliente    = await obtener_datos_cliente(user_id)
    messages         = historial + [{"role": "user", "content": mensaje}]

    con_precios = _pide_precio(mensaje)
    system      = get_system_prompt(con_precios=con_precios, canal=canal)

    if datos_cliente.get("nombre") or datos_cliente.get("telefono"):
        system += "\n\n## Datos conocidos del cliente"
        if datos_cliente.get("nombre"):
            system += f"\nNombre: {datos_cliente['nombre']}"
        if datos_cliente.get("telefono"):
            system += f"\nTeléfono/WA: {datos_cliente['telefono']}"
        system += "\nAntes de generar un nuevo pedido, confirmá con el cliente usando exactamente este formato: \"Voy a generar el pedido a nombre de [nombre], ¿uso el mismo número de teléfono?\" Esperá confirmación antes de cerrar."

    respuesta = await _llamar_claude(messages=messages, system=system)
    if con_precios:
        log.info("Contexto de precios incluido para user=%s", user_id)

    if not respuesta:
        return "Tardamos un poco más de lo normal. ¿Podés repetir tu consulta?"

    await guardar_mensaje(user_id, "user", mensaje)
    await guardar_mensaje(user_id, "assistant", respuesta)

    asyncio.create_task(_intentar_crear_lead(user_id, canal, messages))

    log.info("Claude [%s] user=%s: %s...", canal, user_id, respuesta[:80])
    return respuesta
