"""Tests de lógica pura de telegram_bot.py (parseo de updates, sin llamadas HTTP)."""

from telegram_bot import (extraer_comando_tarea, extraer_contacto, extraer_texto,
                          parsear_comando_tarea, teclado_compartir_telefono, teclado_ocultar)


def _update_texto(chat_id: int, texto: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": texto}}


def _update_contacto(chat_id: int, from_id: int, telefono: str, contacto_user_id: int | None = None) -> dict:
    contacto = {"phone_number": telefono}
    if contacto_user_id is not None:
        contacto["user_id"] = contacto_user_id
    return {"message": {"chat": {"id": chat_id}, "from": {"id": from_id}, "contact": contacto}}


def test_extraer_comando_tarea_simple():
    chat_id, descripcion = extraer_comando_tarea(_update_texto(123, "/tarea Llamar al cliente"))
    assert chat_id == "123"
    assert descripcion == "Llamar al cliente"


def test_extraer_comando_tarea_con_mencion_al_bot():
    """Telegram agrega @nombre_del_bot al comando en grupos: /tarea@mi_bot texto."""
    chat_id, descripcion = extraer_comando_tarea(_update_texto(456, "/tarea@mi_bot Pedir presupuesto"))
    assert chat_id == "456"
    assert descripcion == "Pedir presupuesto"


def test_extraer_comando_tarea_sin_descripcion():
    """El comando solo (sin texto) se reconoce igual — el router responde el
    uso correcto en vez de crear una tarea vacía."""
    chat_id, descripcion = extraer_comando_tarea(_update_texto(123, "/tarea"))
    assert chat_id == "123"
    assert descripcion == ""


def test_extraer_comando_distinto_no_matchea():
    """/tareas (u otro comando) no debe confundirse con /tarea."""
    chat_id, descripcion = extraer_comando_tarea(_update_texto(123, "/tareas listamelas"))
    assert chat_id == ""
    assert descripcion == ""


def test_extraer_comando_tarea_texto_libre_no_matchea():
    chat_id, descripcion = extraer_comando_tarea(_update_texto(123, "hola, cómo estás?"))
    assert chat_id == ""
    assert descripcion == ""


def test_extraer_comando_tarea_update_sin_mensaje():
    """Otros tipos de update de Telegram (ej. edited_message, callback_query) — se ignoran."""
    chat_id, descripcion = extraer_comando_tarea({"edited_message": {"text": "/tarea algo"}})
    assert chat_id == ""
    assert descripcion == ""


def test_extraer_comando_tarea_con_espacios_y_saltos_de_linea():
    chat_id, descripcion = extraer_comando_tarea(_update_texto(123, "/tarea   Revisar stock\ny avisar al depósito  "))
    assert chat_id == "123"
    assert descripcion == "Revisar stock\ny avisar al depósito"


# ─── extraer_texto ──────────────────────────────────────────────────────────────

def test_extraer_texto_cualquier_mensaje():
    chat_id, texto = extraer_texto(_update_texto(123, "hola@ejemplo.com"))
    assert chat_id == "123"
    assert texto == "hola@ejemplo.com"


def test_extraer_texto_update_sin_texto():
    chat_id, texto = extraer_texto({"message": {"chat": {"id": 123}}})
    assert chat_id == ""
    assert texto == ""


# ─── parsear_comando_tarea ───────────────────────────────────────────────────────

def test_parsear_comando_tarea_no_es_comando():
    assert parsear_comando_tarea("juan@ejemplo.com") is None


def test_parsear_comando_tarea_es_comando_vacio():
    assert parsear_comando_tarea("/tarea") == ""


# ─── extraer_contacto ─────────────────────────────────────────────────────────────

def test_extraer_contacto_propio():
    chat_id, telefono = extraer_contacto(_update_contacto(123, from_id=999, telefono="+5493704000000", contacto_user_id=999))
    assert chat_id == "123"
    assert telefono == "+5493704000000"


def test_extraer_contacto_sin_user_id_se_acepta():
    """Algunos clientes de Telegram no mandan contact.user_id — no se puede
    verificar que sea el propio, pero tampoco hay señal de que no lo sea."""
    chat_id, telefono = extraer_contacto(_update_contacto(123, from_id=999, telefono="+5493704000000"))
    assert chat_id == "123"
    assert telefono == "+5493704000000"


def test_extraer_contacto_ajeno_se_descarta():
    """Si alguien reenvía la tarjeta de contacto de otra persona, contact.user_id
    no coincide con from.id — no debe poder registrarse con un teléfono ajeno."""
    chat_id, telefono = extraer_contacto(_update_contacto(123, from_id=999, telefono="+5493704000000", contacto_user_id=111))
    assert chat_id == ""
    assert telefono == ""


def test_extraer_contacto_update_sin_contacto():
    chat_id, telefono = extraer_contacto(_update_texto(123, "hola"))
    assert chat_id == ""
    assert telefono == ""


# ─── teclados ────────────────────────────────────────────────────────────────────

def test_teclado_compartir_telefono_tiene_request_contact():
    teclado = teclado_compartir_telefono()
    assert teclado["keyboard"][0][0]["request_contact"] is True


def test_teclado_ocultar():
    assert teclado_ocultar() == {"remove_keyboard": True}
