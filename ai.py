import asyncio
import json
import logging
import re

import httpx

from config import (ANTHROPIC_API_KEY, PALABRAS_PRECIO, area_bloquea_precios,
                    detectar_areas, get_system_prompt, resolver_destino_odoo)


def _pide_precio(mensaje: str) -> bool:
    texto = mensaje.lower()
    return any(p in texto for p in PALABRAS_PRECIO)


_PALABRAS_ORDEN = {
    "pedido", "orden", "encargo", "estado", "listo", "retiro", "retirar",
    "entrega", "entregan", "terminó", "termino", "terminaron",
    "cuándo está", "cuando esta", "cuándo estará", "ya está", "ya esta",
    "mi pedido", "mi orden", "cómo va", "como va", "avance",
    "puedo pasar", "puedo retirar", "está listo", "esta listo",
}

_NRO_ORDEN_RE = re.compile(r'\b(\d{4,6})\b')


def _consulta_estado_orden(mensaje: str) -> bool:
    texto = mensaje.lower()
    if any(p in texto for p in _PALABRAS_ORDEN):
        return True
    # Mensaje que contiene solo un número de orden (ej: "09425")
    return bool(_NRO_ORDEN_RE.search(texto))


def _extraer_nro_orden(mensaje: str) -> str:
    """Extrae el primer número de 4-6 dígitos del mensaje (posible nro de orden)."""
    m = _NRO_ORDEN_RE.search(mensaje)
    return m.group(1) if m else ""
from db import (buscar_cliente_odoo_por_telefono, buscar_usuario_por_telefono,
                cerrar_conversacion, guardar_datos_cliente, guardar_lead,
                guardar_mensaje, obtener_archivos, obtener_canonical_id,
                obtener_datos_cliente, obtener_historial, tiene_lead_activo,
                vincular_usuario)

log = logging.getLogger(__name__)

EXTRACCION_PROMPT = """
Analizá esta conversación y extraé los datos del cliente.
Respondé SOLO con un JSON válido con este formato exacto (sin explicaciones):
{
  "tiene_lead": true/false,
  "nombre": "nombre y apellido completo del cliente o null",
  "telefono": "teléfono o WhatsApp del cliente o null",
  "email": "email del cliente o null",
  "descripcion": "resumen breve del pedido en 1-2 oraciones o null",
  "requiere_diseno": true/false
}

"tiene_lead" debe ser true SOLO si se cumplen LAS CUATRO condiciones:
1. Se conoce el nombre y apellido del cliente (puede venir de los datos conocidos al inicio)
2. Se conoce el teléfono o WhatsApp del cliente (puede venir de los datos conocidos al inicio)
3. El cliente tiene un pedido o consulta concreta (producto o proyecto definido), con descripción suficiente para registrar el pedido
4. El cliente CONFIRMÓ EXPLÍCITAMENTE el pedido después de que el bot le mostró un resumen y preguntó si estaba correcto (ej: respondió "sí", "dale", "confirmo", "correcto", "así es"). NO alcanza con que el cliente haya dado los datos — tiene que existir esa confirmación posterior en la conversación. Si el bot todavía no pidió confirmación, o la pidió pero el cliente no respondió afirmativamente todavía, "tiene_lead" debe ser false.

Si hay datos conocidos marcados con [Nombre conocido], [Teléfono conocido] o [Email conocido], usarlos por defecto.
EXCEPCIÓN: si el cliente declaró explícitamente datos DISTINTOS en la conversación, usar los datos que el cliente proporcionó.

"requiere_diseno" debe ser true si el cliente pidió diseño desde cero, hablar con el diseñador, hacer un logo, imagen, ilustración o arte que no tiene preparado.
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


async def _intentar_crear_lead(user_id: str, canal: str, historial: list,
                               canonical_id: str | None = None):
    if canonical_id is None:
        canonical_id = await obtener_canonical_id(user_id)
    datos_cliente = await obtener_datos_cliente(canonical_id)

    # Para WA el user_id ES el teléfono — guardarlo si aún no está
    if canal == "whatsapp" and not datos_cliente.get("telefono"):
        await guardar_datos_cliente(canonical_id, telefono=canonical_id)
        datos_cliente["telefono"] = canonical_id

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
    email       = datos.get("email") or ""
    descripcion = datos.get("descripcion") or ""

    # En WhatsApp el user_id ES el número de teléfono
    if canal == "whatsapp" and not telefono:
        telefono = canonical_id

    # Guardia determinística — no confiar solo en el criterio de Claude:
    # sin nombre, teléfono y descripción del pedido, no se crea el lead.
    if not (nombre and telefono and descripcion):
        log.warning(
            "Extracción marcó tiene_lead=true pero faltan datos — no se crea el lead "
            "(canonical=%s, nombre=%r, telefono=%r, descripcion=%r)",
            canonical_id, nombre, telefono, descripcion,
        )
        return

    # Si IG y tenemos teléfono → buscar usuario WA para vincular
    if canal == "instagram" and telefono and canonical_id == user_id:
        wa_id = await buscar_usuario_por_telefono(telefono)
        if wa_id and wa_id != user_id:
            await vincular_usuario(user_id, wa_id)
            canonical_id = wa_id
            log.info("IG user %s vinculado a WA user %s", user_id, wa_id)

    texto_conversacion = " ".join(m["content"] for m in historial if m.get("role") == "user")
    destino         = resolver_destino_odoo(detectar_areas(texto_conversacion))
    requiere_diseno = datos.get("requiere_diseno", False)
    archivos        = await obtener_archivos(canonical_id)

    # Buscar partner Odoo por teléfono para vincular al lead y actualizar email
    odoo_match    = await buscar_cliente_odoo_por_telefono(telefono or canonical_id)
    partner_odoo  = int(odoo_match["odoo_id"]) if odoo_match and odoo_match.get("odoo_id") else None

    from odoo_crm import crear_lead, actualizar_partner
    odoo_id = await crear_lead(
        nombre, telefono, descripcion, canal, user_id,
        historial=historial, archivos=archivos, destino=destino, email=email,
        requiere_diseno=requiere_diseno, partner_id=partner_odoo,
    ) or 0
    await guardar_lead(canonical_id, descripcion, canal, odoo_id)
    await guardar_datos_cliente(canonical_id, nombre=nombre, telefono=telefono, email=email)

    if email and partner_odoo:
        await actualizar_partner(partner_odoo, email=email)

    log.info("Lead creado en Odoo — canonical=%s odoo_id=%s", canonical_id, odoo_id)


_NO_RESPONDER = "NO_RESPONDER"


async def generar_respuesta(user_id: str, mensaje: str, canal: str = "instagram",
                            es_nuevo: bool = False) -> str | None:
    canonical_id = await obtener_canonical_id(user_id)

    if not ANTHROPIC_API_KEY:
        await guardar_mensaje(canonical_id, "user", mensaje)
        return "El servicio de IA no está configurado. Te contactamos a la brevedad."

    historial     = await obtener_historial(canonical_id)
    datos_cliente = await obtener_datos_cliente(canonical_id)

    # Para WA: si no tenemos nombre, buscar en clientes sincronizados de Odoo
    if canal == "whatsapp" and not datos_cliente.get("nombre"):
        odoo_match = await buscar_cliente_odoo_por_telefono(canonical_id)
        if odoo_match and odoo_match.get("nombre"):
            email_odoo = odoo_match.get("email") or ""
            await guardar_datos_cliente(canonical_id, nombre=odoo_match["nombre"], email=email_odoo)
            datos_cliente["nombre"] = odoo_match["nombre"]
            if email_odoo:
                datos_cliente["email"] = email_odoo
            log.info("Cliente WA %s identificado desde Odoo: %s", canonical_id, odoo_match["nombre"])

    messages = historial + [{"role": "user", "content": mensaje}]

    areas         = detectar_areas(mensaje)
    con_precios   = _pide_precio(mensaje) and not area_bloquea_precios(areas)
    consulta_orden = _consulta_estado_orden(mensaje)
    system        = get_system_prompt(con_precios=con_precios, canal=canal, areas_detectadas=areas)

    if consulta_orden:
        from db import (buscar_tareas_por_nombre, buscar_tareas_por_nro_orden,
                        buscar_tareas_por_telefono, obtener_ultima_orden_nro)
        tareas = []

        # 1. Prioridad: número de orden mencionado en el mensaje
        nro_orden = _extraer_nro_orden(mensaje)
        if nro_orden:
            tareas = await buscar_tareas_por_nro_orden(nro_orden)
            if tareas:
                log.info("Tareas encontradas por nro_orden=%s para user=%s", nro_orden, user_id)

        # 2. Fallback: teléfono del cliente
        if not tareas:
            telefono_cliente = datos_cliente.get("telefono") or (canonical_id if canal == "whatsapp" else "")
            if telefono_cliente:
                tareas = await buscar_tareas_por_telefono(telefono_cliente)
                if tareas:
                    log.info("Tareas encontradas por teléfono para user=%s", user_id)

        # 3. Fallback: nombre del cliente registrado en la conversación
        if not tareas and datos_cliente.get("nombre"):
            tareas = await buscar_tareas_por_nombre(datos_cliente["nombre"])
            if tareas:
                log.info("Tareas encontradas por nombre '%s' para user=%s", datos_cliente["nombre"], user_id)

        # 4. Fallback: última orden que Odoo le notificó a este cliente (webhook
        # de "orden confirmada"/"trabajo listo") — cubre el caso típico de "¿cómo
        # va mi pedido?" sin dar número, cuando el teléfono/nombre no matchean
        # exactamente con lo sincronizado desde project.task.
        ultima_orden_nro = await obtener_ultima_orden_nro(canonical_id)
        if not tareas and ultima_orden_nro:
            tareas = await buscar_tareas_por_nro_orden(ultima_orden_nro)
            if tareas:
                log.info("Tareas encontradas por última orden notificada (%s) para user=%s", ultima_orden_nro, user_id)

        if tareas:
            lineas = []
            for t in tareas:
                linea = f"- Pedido {t['nro_orden'] or t['task_name']} | Etapa: {t['stage']}"
                if t.get("partner_name"):
                    linea += f" | Cliente: {t['partner_name']}"
                if t.get("sale_order_name"):
                    linea += f" | Orden: {t['sale_order_name']}"
                lineas.append(linea)
            system += "\n\n## Trabajos del cliente en producción (datos actualizados cada 30 min)\n"
            system += "\n".join(lineas)
            system += (
                "\n\nUsá estos datos para responder sobre el estado del trabajo. "
                "Si la etapa es 'Listo', confirmale que ya puede pasar a retirarlo. "
                "Si está en otra etapa, decile que está en producción y que te va a avisar cuando esté listo."
            )
            log.info("Estado de tareas inyectado para user=%s (%d tarea/s)", user_id, len(tareas))
        elif ultima_orden_nro:
            system += (
                "\n\n## Trabajos del cliente en producción\n"
                f"Su última orden confirmada es la *{ultima_orden_nro}*, pero todavía no hay una etapa "
                "de producción cargada para ella. Decile que su pedido está en preparación y que le "
                f"vamos a avisar apenas esté listo, mencionando el número de orden ({ultima_orden_nro})."
            )
        else:
            system += (
                "\n\n## Trabajos del cliente en producción\n"
                "No se encontraron trabajos para este cliente. "
                "Pedile el número de pedido (ej: 09374) si no lo dio ya, o su nombre completo."
            )

    if es_nuevo:
        if datos_cliente.get("nombre"):
            system += f"\n\nEs el primer mensaje de este cliente — saludalo por su nombre ({datos_cliente['nombre'].split()[0]}), presentate y respondé su consulta en el mismo mensaje."
        else:
            system += "\n\nEs el primer mensaje de este cliente — saludalo cálidamente, presentate y respondé su consulta en el mismo mensaje."

    tiene_datos = datos_cliente.get("nombre") or datos_cliente.get("telefono")
    if tiene_datos:
        system += "\n\n## Datos conocidos del cliente"
        if datos_cliente.get("nombre"):
            system += f"\nNombre: {datos_cliente['nombre']}"
        if datos_cliente.get("telefono"):
            system += f"\nTeléfono/WA: {datos_cliente['telefono']}"
        if datos_cliente.get("email"):
            system += f"\nEmail: {datos_cliente['email']}"
        else:
            system += "\nEmail: (no registrado — pedirlo en algún momento de la conversación)"
        system += (
            "\nDatos de contacto ya conocidos — NO volver a pedirlos a menos que el cliente los corrija."
            "\nSi el cliente corrige algún dato, aceptá el nuevo, confirmalo y registrá el pedido con la info actualizada."
        )

    respuesta = await _llamar_claude(messages=messages, system=system)
    if con_precios:
        log.info("Contexto de precios incluido para user=%s", user_id)

    if not respuesta:
        await guardar_mensaje(canonical_id, "user", mensaje)
        return "Tardamos un poco más de lo normal. ¿Podés repetir tu consulta?"

    if respuesta.strip() == _NO_RESPONDER:
        await guardar_mensaje(canonical_id, "user", mensaje)
        log.info("Sin respuesta automática (saludo/emoji/agradecimiento sin pedido) — canonical=%s", canonical_id)
        return None

    await guardar_mensaje(canonical_id, "user", mensaje)
    await guardar_mensaje(canonical_id, "assistant", respuesta)

    asyncio.create_task(_intentar_crear_lead(user_id, canal, messages, canonical_id))

    # Cierra la conversación solo cuando el bot envió el mensaje de confirmación de lead
    _FRASE_CIERRE = "ya registré tu consulta"
    if _FRASE_CIERRE in respuesta.lower():
        asyncio.create_task(cerrar_conversacion(canonical_id))
        log.info("Conversación cerrada por frase de cierre — canonical=%s", canonical_id)

    log.info("Claude [%s] user=%s: %s...", canal, user_id, respuesta[:80])
    return respuesta
