"""Tests de lógica pura de telegram_bot.py (parseo de updates, sin llamadas HTTP)."""

from telegram_bot import extraer_comando_tarea


def _update_texto(chat_id: int, texto: str) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": texto}}


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
