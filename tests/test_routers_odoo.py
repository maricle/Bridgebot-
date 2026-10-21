"""
Tests de los webhooks de Odoo en main.py — red de seguridad para el split en
routers/ (ver plan de reestructuración). Cubren el comportamiento vigente antes
de mover nada, así el split queda verificado sin cambiar respuestas.

Usan el mismo patrón FakeRequest + unittest.mock.patch usado toda la sesión en
scripts de smoke test sueltos (ahora persistido como test real).
"""

from unittest.mock import AsyncMock, patch

import db
from routers import dashboard_api, webhooks_odoo


class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


async def _seed_cliente(user_id: str, nombre: str = "", telefono: str = "", canal: str = "whatsapp"):
    await db.marcar_saludado(user_id, canal=canal)
    if nombre or telefono:
        await db.guardar_datos_cliente(user_id, nombre=nombre, telefono=telefono or user_id)


# ─── orden-confirmada ──────────────────────────────────────────────────────────

async def test_orden_confirmada_envia_plantilla_presupuesto_2():
    payload = {
        "name": "2026-99999",
        "amount_total": 3600,
        "partner_id": {"id": 1, "display_name": "Maria Test", "phone": "5493794000001"},
    }
    with patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_texto, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_orden_confirmada(FakeRequest(payload))

    assert resultado["ok"] is True
    assert mock_texto.called is False
    args = mock_plantilla.call_args.args
    assert args[1] == "5493794000001"
    assert args[2] == "presupuesto_2"
    assert args[3] == "es_AR"
    nombre, nro_orden, empresa, moneda, monto = args[4]
    assert nombre == "Maria"
    assert nro_orden == "2026-99999"
    assert moneda == "$"
    assert monto == "3.600"


async def test_orden_confirmada_guarda_ultima_orden_del_cliente():
    """Al notificar con éxito, se guarda el order_id como 'orden activa' del
    cliente — así sus mensajes posteriores (ej. el comprobante) se registran
    también en el chatter de esa orden, no solo en el del contacto."""
    payload = {
        "id": 555999,
        "name": "2026-55555",
        "amount_total": 1200,
        "access_url": "/my/orders/555999",
        "partner_id": {"id": 9, "display_name": "Carla Ruiz", "phone": "5493794000009"},
    }
    with patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)), \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_orden_confirmada(FakeRequest(payload))

    assert resultado["ok"] is True
    orden_activa = await db.obtener_ultima_orden("5493794000009")
    assert orden_activa == 555999


async def test_orden_confirmada_resuelve_partner_id_faltante_por_rpc():
    """Si el payload no trae partner_id, se busca la orden completa por RPC y de
    ahí se saca el cliente — cubre el bug real donde el cliente existía en Odoo
    pero el webhook no lo mandaba."""
    payload = {"_model": "sale.order", "id": 555, "display_name": "2026-88888"}
    orden_fake = {
        "id": 555, "name": "2026-88888", "amount_total": 5000,
        "access_url": "/my/orders/555", "partner_id": [42, "Cliente Nuevo"],
    }
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("odoo_crm.obtener_telefono_partner", new=AsyncMock(return_value="5493794999999")), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_orden_confirmada(FakeRequest(payload))

    assert resultado["ok"] is True
    assert resultado["telefono"] == "5493794999999"
    args = mock_plantilla.call_args.args
    assert args[1] == "5493794999999"


async def test_orden_confirmada_sin_telefono_no_envia_y_avisa_en_odoo():
    """Sin partner_id y sin datos para resolverlo por RPC, no hay teléfono — el
    webhook debe devolver ok=False y, como sí hay order_id, avisar en el chatter."""
    payload = {"_model": "sale.order", "id": 777, "name": "2026-77777", "amount_total": 1000}
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=None)), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)) as mock_nota:
        resultado = await webhooks_odoo.webhook_orden_confirmada(FakeRequest(payload))

    assert resultado["ok"] is False
    assert mock_plantilla.called is False
    assert mock_nota.called is True
    assert "no tiene teléfono" in mock_nota.call_args.args[1]


# ─── trabajo-listo (genérico, texto libre) ─────────────────────────────────────

async def test_trabajo_listo_generico_envia_texto_libre():
    payload = {
        "sale_order_id": {"id": 10, "name": "2026-11111"},
        "partner_id": {"id": 2, "display_name": "Juan Perez", "phone": "5493794000002"},
    }
    with patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_texto, \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_trabajo_listo(FakeRequest(payload))

    assert resultado["ok"] is True
    assert mock_plantilla.called is False
    assert mock_texto.called is True
    assert mock_texto.call_args.args[1] == "5493794000002"


# ─── trabajo-listo por sucursal (Taller/Oficina) ───────────────────────────────

async def test_trabajo_listo_taller_usa_su_propia_plantilla_y_direccion():
    """El saldo (x_studio_saldo) vive en sale.order, no en el payload de la
    project.task que dispara el webhook — siempre se busca por RPC."""
    payload = {
        "sale_order_id": {"id": 20, "name": "2026-22222"},
        "partner_id": {"id": 3, "display_name": "Ana Gomez", "phone": "5493794000003"},
    }
    orden_fake = {"id": 20, "name": "2026-22222", "x_studio_saldo": 15000}
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_trabajo_listo_taller(FakeRequest(payload))

    assert resultado["ok"] is True
    args = mock_plantilla.call_args.args
    assert args[2] == "trabajo_listo_taller"
    nombre, nro_orden, saldo, moneda = args[4]
    assert nombre == "Ana"
    assert saldo == "15.000"
    assert moneda == "$"


async def test_trabajo_listo_oficina_usa_su_propia_plantilla():
    payload = {
        "sale_order_id": {"id": 21, "name": "2026-33333"},
        "partner_id": {"id": 4, "display_name": "Luis Diaz", "phone": "5493794000004"},
    }
    orden_fake = {"id": 21, "name": "2026-33333", "x_studio_saldo": 8000}
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_trabajo_listo_oficina(FakeRequest(payload))

    assert resultado["ok"] is True
    args = mock_plantilla.call_args.args
    assert args[2] == "trabajo_listo"


async def test_trabajo_listo_taller_resuelve_partner_id_faltante_por_rpc():
    payload = {"sale_order_id": {"id": 30}}
    orden_fake = {
        "id": 30, "name": "2026-44444", "x_studio_saldo": 2000,
        "partner_id": [55, "Cliente Sin Telefono En Payload"],
    }
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("odoo_crm.obtener_telefono_partner", new=AsyncMock(return_value="5493794000005")), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_trabajo_listo_taller(FakeRequest(payload))

    assert resultado["ok"] is True
    assert resultado["telefono"] == "5493794000005"


async def test_trabajo_listo_taller_usa_saldo_no_monto_total():
    """El mensaje y la plantilla tienen que usar x_studio_saldo (lo que falta
    pagar), no amount_total — una orden con seña ya pagada debe mostrar el
    saldo remanente, no el total original del pedido."""
    payload = {"sale_order_id": {"id": 22, "name": "2026-55555"},
               "partner_id": {"id": 6, "display_name": "Marcos Ruiz", "phone": "5493794000006"}}
    orden_fake = {"id": 22, "name": "2026-55555", "amount_total": 20000, "x_studio_saldo": 10000}
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)) as mock_nota:
        resultado = await webhooks_odoo.webhook_trabajo_listo_taller(FakeRequest(payload))

    assert resultado["ok"] is True
    _, _, saldo, _ = mock_plantilla.call_args.args[4]
    assert saldo == "10.000"
    assert "Saldo de *$10.000*" in mock_nota.call_args.args[1]


async def test_trabajo_listo_oficina_sale_order_id_como_int_plano():
    """La acción nativa "Enviar webhook" de Odoo manda los many2one seleccionados
    como id plano (no [id, "nombre"]) cuando el campo no se expande — payload
    real observado: sale_order_id=193448, partner_id=False. _resolver_order_id
    tiene que resolverlo igual, y el resto de los datos se completan por RPC."""
    payload = {"id": 26852, "partner_id": False, "sale_order_id": 193448, "sale_order_state": "sale"}
    orden_fake = {
        "id": 193448, "name": "2026-12188", "x_studio_saldo": 18595.04,
        "partner_id": [38934, "maria prueba test"],
    }
    with patch("odoo_crm.buscar_orden_por_id", new=AsyncMock(return_value=orden_fake)), \
         patch("odoo_crm.obtener_telefono_partner", new=AsyncMock(return_value="543794579133")), \
         patch("whatsapp.enviar_plantilla", new=AsyncMock(return_value=True)) as mock_plantilla, \
         patch("odoo_crm.registrar_nota_orden", new=AsyncMock(return_value=True)):
        resultado = await webhooks_odoo.webhook_trabajo_listo_oficina(FakeRequest(payload))

    assert resultado["ok"] is True
    assert resultado["telefono"] == "543794579133"
    assert resultado["orden"] == "2026-12188"
    assert mock_plantilla.called is True


# ─── /responder — enrutamiento WhatsApp vs Instagram ───────────────────────────

async def test_responder_usa_whatsapp_por_defecto():
    await _seed_cliente("5493794000006", telefono="5493794000006", canal="whatsapp")
    body = {"user_id": "5493794000006", "mensaje": "hola"}
    with patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_wa, \
         patch("instagram.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_ig:
        resultado = await dashboard_api.responder_whatsapp(FakeRequest(body))

    assert resultado == {"ok": True}
    assert mock_wa.called is True
    assert mock_ig.called is False


async def test_responder_usa_instagram_si_el_cliente_es_de_ese_canal():
    await _seed_cliente("ig_test_user_1", nombre="Cliente IG", canal="instagram")
    body = {"user_id": "ig_test_user_1", "mensaje": "hola"}
    with patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_wa, \
         patch("instagram.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_ig:
        resultado = await dashboard_api.responder_whatsapp(FakeRequest(body))

    assert resultado == {"ok": True}
    assert mock_ig.called is True
    assert mock_wa.called is False


async def test_responder_respeta_canal_explicito_del_body():
    body = {"user_id": "5493794000007", "mensaje": "hola", "canal": "instagram"}
    with patch("whatsapp.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_wa, \
         patch("instagram.enviar_mensaje", new=AsyncMock(return_value=True)) as mock_ig:
        resultado = await dashboard_api.responder_whatsapp(FakeRequest(body))

    assert resultado == {"ok": True}
    assert mock_ig.called is True
    assert mock_wa.called is False
