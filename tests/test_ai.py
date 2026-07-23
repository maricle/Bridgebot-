"""
Tests del sentinel NO_RESPONDER: el bot no debe enviar nada cuando el
mensaje del cliente es solo un saludo, emoji o agradecimiento sin
ningún pedido concreto — esos casos se atienden desde otra aplicación.

También cubre la guardia determinística de _intentar_crear_lead: nunca
crear un lead en Odoo si falta nombre, teléfono o descripción del
pedido, sin importar lo que haya decidido la extracción de Claude.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

import ai
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
