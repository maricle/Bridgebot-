import logging

import httpx

from config import GROQ_API_KEY, SYSTEM_PROMPT
from db import guardar_lead, guardar_mensaje, obtener_historial

log = logging.getLogger(__name__)

PALABRAS_LEAD = [
    "presupuesto", "precio", "cuánto", "cuanto", "cotización",
    "medidas", "cantidad", "encargar", "necesito", "quiero",
]


async def generar_respuesta(user_id: str, mensaje: str, canal: str = "instagram") -> str:
    if not GROQ_API_KEY:
        return "El servicio de IA no está configurado. Te contactamos a la brevedad."

    await guardar_mensaje(user_id, "user", mensaje)
    msgs = await obtener_historial(user_id)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + msgs,
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
                timeout=25,
            )

            if resp.status_code != 200:
                log.error("Groq error %s: %s", resp.status_code, resp.text)
                return "En este momento no puedo responderte. Te contactamos a la brevedad."

            respuesta = resp.json()["choices"][0]["message"]["content"].strip()

    except httpx.TimeoutException:
        log.error("Groq timeout — user=%s", user_id)
        return "Tardamos un poco más de lo normal. ¿Podés repetir tu consulta?"
    except Exception as e:
        log.error("Groq excepción — user=%s: %s", user_id, e)
        return "Tuvimos un problema técnico. Te contactamos a la brevedad."

    await guardar_mensaje(user_id, "assistant", respuesta)

    if any(p in mensaje.lower() for p in PALABRAS_LEAD):
        await guardar_lead(user_id, f"Mensaje: {mensaje[:200]}", canal)

    log.info("Groq [%s] user=%s: %s...", canal, user_id, respuesta[:80])
    return respuesta
