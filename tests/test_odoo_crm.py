"""
Tests de lógica pura de odoo_crm.py (sin llamadas HTTP a Odoo).

Cubre los patrones de falla observados en el historial de chats:

1. _extraer_links:
   - Links de Pinterest enviados como texto plano (filas 145, 211, 389-413 del DB)
   - Múltiples links en la misma conversación → todos capturados
   - Mismo link enviado dos veces → aparece una sola vez
   - Links del asistente → ignorados (solo se capturan los del usuario)

2. _transcripcion_html:
   - Contenido con HTML especial → escapado correctamente (previene XSS en Odoo)
   - Historial vacío → string vacío
"""

import uuid
from unittest.mock import AsyncMock, patch

import db
import odoo_crm
from odoo_crm import _extraer_links, _transcripcion_html


# ─── _extraer_links ───────────────────────────────────────────────────────────

def test_links_pinterest_en_mensaje_con_emoji():
    """Fila 145 del DB: usuario envía 'Take a look! 👀 https://pin.it/2hgK6XPaM'."""
    historial = [
        {"role": "user",      "content": "Take a look! 👀 https://pin.it/2hgK6XPaM"},
        {"role": "assistant", "content": "No puedo acceder a ese link desde acá."},
    ]
    links = _extraer_links(historial)
    assert "https://pin.it/2hgK6XPaM" in links


def test_links_pinterest_solo_url():
    """Fila 211 del DB: usuario envía solo la URL, sin texto adicional."""
    historial = [{"role": "user", "content": "https://pin.it/5H5RtMrLT"}]
    links = _extraer_links(historial)
    assert links == ["https://pin.it/5H5RtMrLT"]


def test_links_burst_multiples_pinterest():
    """
    Filas 389-413 del DB: usuario 5493705137369 envía 5 links de Pinterest
    en ráfaga. Todos deben aparecer en el lead de Odoo.
    """
    historial = [
        {"role": "user", "content": "https://pin.it/2hgK6XPaM"},
        {"role": "user", "content": "https://pin.it/5H5RtMrLT"},
        {"role": "user", "content": "https://pin.it/3AbCdEfGh"},
        {"role": "user", "content": "https://pin.it/7XyZwVuTu"},
        {"role": "user", "content": "https://pin.it/1QrStUvWx"},
    ]
    links = _extraer_links(historial)
    assert len(links) == 5
    assert "https://pin.it/2hgK6XPaM" in links
    assert "https://pin.it/1QrStUvWx" in links


def test_links_sin_duplicados():
    """El usuario manda el mismo link dos veces → aparece una sola vez."""
    historial = [
        {"role": "user", "content": "Acá el diseño: https://pin.it/2hgK6XPaM"},
        {"role": "user", "content": "Te lo repito: https://pin.it/2hgK6XPaM"},
    ]
    links = _extraer_links(historial)
    assert links.count("https://pin.it/2hgK6XPaM") == 1


def test_links_ignora_mensajes_asistente():
    """El asistente menciona una URL (ej. instrucciones) → no se captura en links."""
    historial = [
        {"role": "assistant", "content": "Podés subir tu archivo en https://wetransfer.com"},
        {"role": "user",      "content": "Acá está: https://pin.it/2hgK6XPaM"},
    ]
    links = _extraer_links(historial)
    assert "https://wetransfer.com" not in links
    assert "https://pin.it/2hgK6XPaM" in links


def test_links_sin_urls():
    historial = [
        {"role": "user", "content": "Necesito 10 copias A3"},
        {"role": "user", "content": "En color, con plastificado"},
    ]
    assert _extraer_links(historial) == []


def test_links_historial_vacio():
    assert _extraer_links([]) == []


def test_links_drive_y_pinterest_juntos():
    historial = [
        {"role": "user", "content": "El diseño está acá: https://drive.google.com/file/d/abc123/view"},
        {"role": "user", "content": "Y la referencia acá: https://pin.it/2hgK6XPaM"},
    ]
    links = _extraer_links(historial)
    assert len(links) == 2
    assert any("drive.google.com" in l for l in links)
    assert any("pin.it" in l for l in links)


def test_links_multiples_en_un_mensaje():
    historial = [
        {"role": "user", "content":
            "Mirá estos dos: https://pin.it/aaa https://pin.it/bbb"},
    ]
    links = _extraer_links(historial)
    assert "https://pin.it/aaa" in links
    assert "https://pin.it/bbb" in links


# ─── _transcripcion_html ─────────────────────────────────────────────────────

def test_transcripcion_escapa_html():
    """Previene XSS en el body del lead de Odoo."""
    historial = [
        {"role": "user",      "content": "<script>alert('xss')</script>"},
        {"role": "assistant", "content": "Lo lamento, no entendí"},
    ]
    html = _transcripcion_html(historial)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_transcripcion_escapa_ampersand():
    historial = [{"role": "user", "content": "Talle S & M"}]
    html = _transcripcion_html(historial)
    assert "&amp;" in html
    assert " & " not in html


def test_transcripcion_vacia():
    assert _transcripcion_html([]) == ""


def test_transcripcion_contiene_cliente_y_bot():
    historial = [
        {"role": "user",      "content": "Hola, necesito tarjetas"},
        {"role": "assistant", "content": "¡Claro! ¿Cuántas necesitás?"},
    ]
    html = _transcripcion_html(historial)
    assert "Cliente:" in html
    assert "Asistente:" in html


def test_transcripcion_newlines_a_br():
    historial = [{"role": "user", "content": "línea 1\nlínea 2"}]
    html = _transcripcion_html(historial)
    assert "<br/>" in html


# ─── notas: contacto + orden activa ────────────────────────────────────────────
# Después de "orden confirmada" / "trabajo listo", los mensajes posteriores del
# cliente (ej. el comprobante de pago) deben aparecer también en el chatter de
# esa orden en Odoo, no solo en el del contacto (pedido explícito del usuario).

def _telefono() -> str:
    return "549379" + str(uuid.uuid4().int)[:10]


async def _seed_cliente_sincronizado(telefono: str, odoo_id: int) -> str:
    """Da de alta un cliente de WhatsApp con su contraparte ya sincronizada en
    clientes_odoo — condición para que las funciones de notas hagan algo."""
    await db.marcar_saludado(telefono, canal="whatsapp")
    await db.guardar_datos_cliente(telefono, telefono=telefono)
    await db.upsert_clientes_odoo([{"odoo_id": odoo_id, "nombre": "Cliente Test", "telefono": telefono}])
    return telefono


async def test_registrar_mensaje_historial_solo_contacto_sin_orden_activa():
    tel = await _seed_cliente_sincronizado(_telefono(), odoo_id=901)
    with patch("odoo_crm.registrar_nota", new=AsyncMock(return_value=True)) as mock_nota, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)) as mock_nota_orden:
        await odoo_crm.registrar_mensaje_historial(tel, "user", "hola")

    assert mock_nota.called is True
    assert mock_nota.call_args.args[0] == "res.partner"
    assert mock_nota_orden.called is False


async def test_registrar_mensaje_historial_tambien_va_a_la_orden_activa():
    tel = await _seed_cliente_sincronizado(_telefono(), odoo_id=902)
    await db.actualizar_ultima_orden(tel, 4321)
    with patch("odoo_crm.registrar_nota", new=AsyncMock(return_value=True)) as mock_nota, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)) as mock_nota_orden:
        await odoo_crm.registrar_mensaje_historial(tel, "user", "les mando el comprobante")

    assert mock_nota.called is True
    assert mock_nota_orden.called is True
    assert mock_nota_orden.call_args.args[0] == 4321
    assert mock_nota_orden.call_args.args[1] == mock_nota.call_args.args[2]


async def test_notificar_comprobante_pago_tambien_va_a_la_orden_activa():
    tel = await _seed_cliente_sincronizado(_telefono(), odoo_id=903)
    await db.actualizar_ultima_orden(tel, 5678)
    with patch("odoo_crm.registrar_nota", new=AsyncMock(return_value=True)) as mock_nota, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)) as mock_nota_orden:
        await odoo_crm.notificar_comprobante_pago(tel, {"monto": "1000", "banco": "Galicia"})

    assert mock_nota.called is True
    assert mock_nota_orden.called is True
    assert mock_nota_orden.call_args.args[0] == 5678
