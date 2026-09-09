"""Tests del webhook /webhook/telegram — bot interno, comando /tarea."""

from unittest.mock import AsyncMock, patch

from routers import webhooks_telegram


class FakeRequest:
    def __init__(self, payload: dict, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _update_texto(chat_id, texto: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": texto}}


async def test_tarea_crea_en_odoo_y_confirma():
    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=42)) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(
            FakeRequest(_update_texto(111, "/tarea Llamar al proveedor"))
        )

    assert resultado == {"ok": True}
    mock_crear.assert_called_once_with("Llamar al proveedor")
    assert mock_envio.called is True
    assert "42" in mock_envio.call_args.args[2]


async def test_tarea_sin_descripcion_pide_uso():
    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=1)) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(111, "/tarea")))

    assert mock_crear.called is False
    assert "Uso:" in mock_envio.call_args.args[2]


async def test_mensaje_sin_comando_se_ignora_silenciosamente():
    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock()) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock()) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(111, "hola")))

    assert resultado == {"ok": True}
    assert mock_crear.called is False
    assert mock_envio.called is False


async def test_chat_no_autorizado_se_rechaza():
    with patch("routers.webhooks_telegram.TELEGRAM_CHAT_ID", "999"), \
         patch("odoo_crm.crear_tarea_personal", new=AsyncMock()) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(111, "/tarea algo")))

    assert mock_crear.called is False
    assert "No autorizado" in mock_envio.call_args.args[2]


async def test_secreto_de_webhook_invalido_se_ignora():
    with patch("routers.webhooks_telegram.TELEGRAM_WEBHOOK_SECRET", "shhh"), \
         patch("odoo_crm.crear_tarea_personal", new=AsyncMock()) as mock_crear:
        resultado = await webhooks_telegram.recibir_telegram(
            FakeRequest(_update_texto(111, "/tarea algo"), headers={"X-Telegram-Bot-Api-Secret-Token": "otro"})
        )

    assert resultado == {"ok": True}
    assert mock_crear.called is False


async def test_odoo_no_disponible_avisa_error():
    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=None)), \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(111, "/tarea algo")))

    assert "no se pudo crear" in mock_envio.call_args.args[2].lower()
