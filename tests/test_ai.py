"""
Tests del sentinel NO_RESPONDER: el bot no debe enviar nada cuando el
mensaje del cliente es solo un saludo, emoji o agradecimiento sin
ningún pedido concreto — esos casos se atienden desde otra aplicación.
"""

from unittest.mock import AsyncMock, patch

import pytest

import ai


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
