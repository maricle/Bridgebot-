"""
Tests del sentinel NO_RESPONDER: el bot no debe enviar nada cuando el
mensaje del cliente es solo un saludo, emoji o agradecimiento sin
ningún pedido concreto — esos casos se atienden desde otra aplicación.

También cubre la guardia determinística de _intentar_crear_lead: nunca
crear un lead en Odoo si falta nombre, teléfono o descripción del
pedido, sin importar lo que haya decidido la extracción de Claude.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

import ai
import db
from db import resetear_usuario


@pytest.mark.asyncio
async def test_no_responder_cuando_claude_devuelve_el_sentinel():
    with patch("ai._llamar_claude", new_callable=AsyncMock, return_value="NO_RESPONDER"):
        respuesta = await ai.generar_respuesta("5493700000001", "hola!! 👋", canal="whatsapp")
    assert respuesta is None


@pytest.mark.asyncio
async def test_responde_normal_cuando_hay_pedido_concreto():
    with patch("ai._llamar_claude", new_callable=AsyncMock,
               return_value="Buenísimo, ¿qué medidas necesitás?"):
        respuesta = await ai.generar_respuesta("5493700000002", "necesito laqueado de una placa", canal="whatsapp")
    assert respuesta == "Buenísimo, ¿qué medidas necesitás?"


def _extraccion(**overrides) -> str:
    base = {"tiene_lead": True, "nombre": "Juan Perez", "telefono": "5493700000099",
            "descripcion": "lona 2x1 confirmada", "email": None, "requiere_diseno": False}
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_no_crea_lead_si_falta_descripcion_pese_a_tiene_lead_true():
    await resetear_usuario("5493700000010")
    with patch("ai._llamar_claude", new_callable=AsyncMock, return_value=_extraccion(descripcion=None)), \
         patch("odoo_crm.crear_lead", new_callable=AsyncMock) as mock_crear_lead:
        await ai._intentar_crear_lead("5493700000010", "whatsapp", [{"role": "user", "content": "hola"}])
    mock_crear_lead.assert_not_called()


@pytest.mark.asyncio
async def test_no_crea_lead_si_falta_nombre_pese_a_tiene_lead_true():
    await resetear_usuario("5493700000011")
    with patch("ai._llamar_claude", new_callable=AsyncMock, return_value=_extraccion(nombre=None)), \
         patch("odoo_crm.crear_lead", new_callable=AsyncMock) as mock_crear_lead:
        await ai._intentar_crear_lead("5493700000011", "whatsapp", [{"role": "user", "content": "hola"}])
    mock_crear_lead.assert_not_called()


@pytest.mark.asyncio
async def test_no_crea_lead_si_falta_telefono_pese_a_tiene_lead_true():
    # Instagram: a diferencia de WhatsApp, el telefono no se autocompleta desde el user_id.
    await resetear_usuario("ig_user_000012")
    with patch("ai._llamar_claude", new_callable=AsyncMock, return_value=_extraccion(telefono=None)), \
         patch("odoo_crm.crear_lead", new_callable=AsyncMock) as mock_crear_lead:
        await ai._intentar_crear_lead("ig_user_000012", "instagram", [{"role": "user", "content": "hola"}])
    mock_crear_lead.assert_not_called()


@pytest.mark.asyncio
async def test_crea_lead_cuando_estan_los_tres_datos_completos():
    await resetear_usuario("5493700000013")
    with patch("ai._llamar_claude", new_callable=AsyncMock, return_value=_extraccion()), \
         patch("odoo_crm.crear_lead", new_callable=AsyncMock, return_value=123) as mock_crear_lead:
        await ai._intentar_crear_lead("5493700000013", "whatsapp", [{"role": "user", "content": "confirmo el pedido"}])
    mock_crear_lead.assert_called_once()


# ─── consulta de estado de orden: fallback a la última orden notificada ────────
# El cliente suele preguntar "¿cómo va mi pedido?" sin dar el número — si el
# teléfono/nombre no matchean con lo sincronizado desde project.task, el bot
# debe igual poder usar la última orden que Odoo le confirmó/notificó por
# WhatsApp (guardada en actualizar_ultima_orden) en vez de no encontrar nada.

def _telefono() -> str:
    return "549379" + str(uuid.uuid4().int)[:10]


@pytest.mark.asyncio
async def test_consulta_orden_usa_ultima_orden_notificada_como_fallback():
    tel = _telefono()
    nro = f"2026-{str(uuid.uuid4().int)[:5]}"
    odoo_id = uuid.uuid4().int % 1_000_000
    await db.actualizar_ultima_orden(tel, odoo_id, nro)
    # Teléfono y nombre de la tarea sincronizada NO matchean con el cliente que
    # pregunta — así el fallback #2 (teléfono) y #3 (nombre) fallan a propósito
    # y el único camino que puede encontrar la tarea es el de última orden (#4).
    await db.upsert_tareas_odoo([{
        "odoo_id": odoo_id, "task_name": f"{nro} - Cliente Random", "nro_orden": nro,
        "stage": "En producción", "partner_name": "Cliente Random",
        "telefono": "5493700000000", "documento": "", "sale_order_name": nro,
    }])

    with patch("ai._llamar_claude", new=AsyncMock(return_value="Tu pedido está en producción")) as mock_claude:
        await ai.generar_respuesta(tel, "cómo va mi pedido?", canal="whatsapp")

    system = mock_claude.call_args.kwargs["system"]
    assert nro in system
    assert "En producción" in system


@pytest.mark.asyncio
async def test_consulta_orden_sin_tareas_pero_con_ultima_orden_menciona_el_numero():
    """Sin ninguna tarea sincronizada todavía (recién confirmada), el bot no debe
    pedirle el número de pedido al cliente si ya lo tiene guardado — debe
    mencionárselo y avisar que está en preparación."""
    tel = _telefono()
    nro = f"2026-{str(uuid.uuid4().int)[:5]}"
    odoo_id = uuid.uuid4().int % 1_000_000
    await db.actualizar_ultima_orden(tel, odoo_id, nro)

    with patch("ai._llamar_claude", new=AsyncMock(return_value="listo")) as mock_claude:
        await ai.generar_respuesta(tel, "cómo va mi pedido?", canal="whatsapp")

    system = mock_claude.call_args.kwargs["system"]
    assert nro in system
    assert "Pedile el número de pedido" not in system
