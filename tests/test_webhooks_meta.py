"""
Tests de procesar_whatsapp/procesar_instagram — red de seguridad para moverlos
a routers/webhooks_meta.py. Es el flujo de mayor riesgo (conversación real con
clientes), cubierto acá antes del split para poder comparar el comportamiento.
"""

import uuid
from unittest.mock import AsyncMock, patch

import db
from routers import webhooks_meta


def _telefono() -> str:
    """Igual que _wamid(): la DB persiste entre corridas, asi que un numero
    fijo dejaria de ser 'nuevo' (es_usuario_nuevo) en la segunda corrida."""
    return "549379" + str(uuid.uuid4().int)[:10]


def _wamid() -> str:
    """La DB de test es persistente entre corridas (tests/conftest.py usa un
    archivo fijo) — un message_id hardcodeado quedaría marcado como procesado
    para siempre y el test de dedup fallaría en la segunda corrida."""
    return f"wamid.{uuid.uuid4()}"


def _payload_texto_wa(sender: str, texto: str, message_id: str | None = None) -> dict:
    return {
        "entry": [{"changes": [{"value": {"messages": [{
            "from": sender, "id": message_id or _wamid(), "type": "text",
            "text": {"body": texto},
        }]}}]}]
    }


def _payload_documento_wa(sender: str, media_id: str, mime_type: str, message_id: str | None = None) -> dict:
    return {
        "entry": [{"changes": [{"value": {"messages": [{
            "from": sender, "id": message_id or _wamid(), "type": "document",
            "document": {"id": media_id, "mime_type": mime_type},
        }]}}]}]
    }


async def _esperar_tareas_pendientes():
    import asyncio
    tareas = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tareas:
        import contextlib
        with contextlib.suppress(Exception):
            await asyncio.gather(*tareas)


# ─── mensaje de texto normal ────────────────────────────────────────────────────

async def test_wa_mensaje_normal_llama_a_claude_y_responde():
    tel = _telefono()
    payload = _payload_texto_wa(tel, "Hola, precio de MDF?")
    with patch("routers.webhooks_meta.generar_respuesta", new=AsyncMock(return_value="Hola! el MDF...")) as mock_claude, \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_meta.procesar_whatsapp(payload)

    assert mock_claude.called is True
    assert mock_envio.called is True
    assert mock_envio.call_args.args[1] == tel
    assert mock_envio.call_args.args[2] == "Hola! el MDF..."
    # El guardado del mensaje del usuario/asistente vive dentro de
    # ai.generar_respuesta (acá mockeado), no en procesar_whatsapp.


async def test_wa_usuario_pausado_no_llama_a_claude():
    tel = _telefono()
    await db.marcar_saludado(tel, canal="whatsapp")
    await db.pausar_usuario(tel)
    payload = _payload_texto_wa(tel, "hola")
    with patch("routers.webhooks_meta.generar_respuesta", new=AsyncMock(return_value="no debería llamarse")) as mock_claude, \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_meta.procesar_whatsapp(payload)

    assert mock_claude.called is False
    historial = await db.obtener_conversacion(tel)
    assert any(m["contenido"] == "hola" for m in historial)


async def test_wa_mensaje_duplicado_se_ignora():
    mid = _wamid()
    payload = _payload_texto_wa(_telefono(), "hola de nuevo", mid)
    with patch("routers.webhooks_meta.generar_respuesta", new=AsyncMock(return_value="respuesta")) as mock_claude, \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_meta.procesar_whatsapp(payload)
        await webhooks_meta.procesar_whatsapp(payload)  # mismo message_id

    assert mock_claude.call_count == 1


async def test_wa_auto_respuesta_manda_saludo_sin_llamar_a_claude():
    payload = _payload_texto_wa(_telefono(), "hola")
    with patch("config.AUTO_RESPUESTA", True), \
         patch("routers.webhooks_meta.generar_respuesta", new=AsyncMock(return_value="no debería llamarse")) as mock_claude, \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_meta.procesar_whatsapp(payload)

    assert mock_claude.called is False
    assert mock_envio.called is True


async def test_wa_cliente_nuevo_se_completa_con_datos_de_odoo():
    tel = _telefono()
    payload = _payload_texto_wa(tel, "hola")
    odoo_match = {"odoo_id": 1, "nombre": "Cliente Odoo", "email": "x@x.com"}
    with patch("routers.webhooks_meta.buscar_cliente_odoo_por_telefono", new=AsyncMock(return_value=odoo_match)), \
         patch("routers.webhooks_meta.generar_respuesta", new=AsyncMock(return_value="hola!")), \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_meta.procesar_whatsapp(payload)

    datos = await db.obtener_datos_cliente(tel)
    assert datos["nombre"] == "Cliente Odoo"


# ─── archivos ───────────────────────────────────────────────────────────────────

async def test_wa_archivo_generico_se_guarda_y_confirma():
    payload = _payload_documento_wa(_telefono(), "media123", "application/msword")
    with patch("odoo_crm.registrar_nota", new=AsyncMock(return_value=True)), \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_meta.procesar_whatsapp(payload)
        await _esperar_tareas_pendientes()

    assert mock_envio.called is True
    assert "Recibimos el archivo" in mock_envio.call_args.args[2]
