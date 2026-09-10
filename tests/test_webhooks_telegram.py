"""Tests del webhook /webhook/telegram — bot interno: alta de usuario
(validada contra Odoo) y comando /tarea."""

import uuid
from unittest.mock import AsyncMock, patch

import db
from routers import webhooks_telegram


class FakeRequest:
    def __init__(self, payload: dict, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _chat_id() -> int:
    """chat_id único por test — la tabla telegram_usuarios persiste entre
    tests de la misma sesión de pytest (ver tests/conftest.py)."""
    return int(str(uuid.uuid4().int)[:10])


def _update_texto(chat_id, texto: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": texto}}


def _update_contacto(chat_id, telefono: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "from": {"id": chat_id},
                        "contact": {"phone_number": telefono, "user_id": chat_id}}}


async def _registrar(chat_id, nombre: str = "Test User", odoo_user_id: int = 1):
    with patch("odoo_crm.validar_usuario", new=AsyncMock(return_value={"odoo_user_id": odoo_user_id, "nombre": nombre})), \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_contacto(chat_id, "+5493700000099")))
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "user@ejemplo.com")))


# ─── /start ──────────────────────────────────────────────────────────────────

async def test_start_usuario_no_registrado_pide_telefono():
    chat_id = _chat_id()
    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/start")))

    assert resultado == {"ok": True}
    assert mock_envio.called is True
    assert "teléfono" in mock_envio.call_args.args[2].lower()
    teclado = mock_envio.call_args.kwargs.get("teclado")
    assert teclado["keyboard"][0][0]["request_contact"] is True


async def test_start_usuario_ya_registrado_avisa():
    chat_id = _chat_id()
    await _registrar(chat_id)

    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/start")))

    assert "ya estás registrado" in mock_envio.call_args.args[2].lower()


# ─── alta ──────────────────────────────────────────────────────────────────────

async def test_contacto_compartido_pide_correo():
    chat_id = _chat_id()
    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_contacto(chat_id, "+5493700000002")))

    assert "correo" in mock_envio.call_args.args[2].lower()


async def test_correo_valido_registra_usuario():
    chat_id = _chat_id()
    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_contacto(chat_id, "+5493700000003")))

    with patch("odoo_crm.validar_usuario", new=AsyncMock(return_value={"odoo_user_id": 7, "nombre": "Maria Bertoli"})) as mock_validar, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "maria@grupoideas.com")))

    assert resultado == {"ok": True}
    mock_validar.assert_called_once_with("maria@grupoideas.com", "+5493700000003")
    assert "validado" in mock_envio.call_args.args[2].lower()
    assert "Maria Bertoli" in mock_envio.call_args.args[2]

    usuario = await db.obtener_telegram_usuario(str(chat_id))
    assert usuario["nombre"] == "Maria Bertoli"
    assert usuario["odoo_user_id"] == 7


async def test_correo_invalido_no_registra():
    chat_id = _chat_id()
    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)):
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_contacto(chat_id, "+5493700000004")))

    with patch("odoo_crm.validar_usuario", new=AsyncMock(return_value=None)), \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "nadie@ejemplo.com")))

    assert "no pude validar" in mock_envio.call_args.args[2].lower()
    assert await db.telegram_usuario_registrado(str(chat_id)) is False


# ─── /tarea ──────────────────────────────────────────────────────────────────

async def test_tarea_sin_registro_pide_start():
    chat_id = _chat_id()
    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=1)) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/tarea Llamar al cliente")))

    assert mock_crear.called is False
    assert "/start" in mock_envio.call_args.args[2]


async def test_tarea_crea_en_odoo_con_solicitante():
    chat_id = _chat_id()
    await _registrar(chat_id, nombre="Ana Gómez")

    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=42)) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/tarea Llamar al proveedor")))

    assert resultado == {"ok": True}
    mock_crear.assert_called_once_with("Llamar al proveedor", solicitante="Ana Gómez")
    assert "42" in mock_envio.call_args.args[2]


async def test_tarea_sin_descripcion_pide_uso():
    chat_id = _chat_id()
    await _registrar(chat_id)

    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=1)) as mock_crear, \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/tarea")))

    assert mock_crear.called is False
    assert "Uso:" in mock_envio.call_args.args[2]


async def test_odoo_no_disponible_avisa_error():
    chat_id = _chat_id()
    await _registrar(chat_id)

    with patch("odoo_crm.crear_tarea_personal", new=AsyncMock(return_value=None)), \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "/tarea algo")))

    assert "no se pudo crear" in mock_envio.call_args.args[2].lower()


# ─── otros casos ────────────────────────────────────────────────────────────────

async def test_mensaje_sin_comando_de_chat_no_registrado_pide_start():
    chat_id = _chat_id()
    with patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(FakeRequest(_update_texto(chat_id, "hola")))

    assert resultado == {"ok": True}
    assert "/start" in mock_envio.call_args.args[2]


async def test_secreto_de_webhook_invalido_se_ignora():
    chat_id = _chat_id()
    with patch("routers.webhooks_telegram.TELEGRAM_WEBHOOK_SECRET", "shhh"), \
         patch("telegram_bot.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_envio:
        resultado = await webhooks_telegram.recibir_telegram(
            FakeRequest(_update_texto(chat_id, "/start"), headers={"X-Telegram-Bot-Api-Secret-Token": "otro"})
        )

    assert resultado == {"ok": True}
    assert mock_envio.called is False
