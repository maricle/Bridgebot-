import logging

import httpx

from config import ODOO_API_KEY, ODOO_DB, ODOO_URL, ODOO_LOGIN

log = logging.getLogger(__name__)

JSONRPC_URL = f"{ODOO_URL}/jsonrpc" if ODOO_URL else ""


async def _autenticar(client: httpx.AsyncClient) -> int | None:
    """Retorna el uid numérico del usuario autenticado."""
    try:
        resp = await client.post(
            JSONRPC_URL,
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "id": 1,
                "params": {
                    "service": "common",
                    "method": "authenticate",
                    "args": [ODOO_DB, ODOO_LOGIN, ODOO_API_KEY, {}],
                },
            },
            timeout=15,
        )
        data = resp.json()
        uid = data.get("result")
        if not uid:
            log.error("Odoo auth fallida: %s", data.get("error"))
            return None
        return uid
    except Exception as e:
        log.error("Error autenticando en Odoo: %s", e)
        return None


async def _execute_kw(client: httpx.AsyncClient, uid: int, model: str,
                      method: str, args: list, kwargs: dict = {}) -> any:
    resp = await client.post(
        JSONRPC_URL,
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "id": 2,
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [ODOO_DB, uid, ODOO_API_KEY, model, method, args, kwargs],
            },
        },
        timeout=15,
    )
    data = resp.json()
    if "error" in data:
        raise Exception(f"Odoo RPC error: {data['error']}")
    return data["result"]


async def crear_lead(nombre_cliente: str, telefono: str, descripcion: str,
                     canal: str = "instagram", user_id: str = "") -> int | None:
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        log.warning("Odoo CRM no configurado — lead no creado")
        return None

    titulo = f"[{canal.upper()}] {nombre_cliente or 'Cliente sin nombre'}"
    cuerpo = (
        f"Canal: {canal}\n"
        f"ID usuario: {user_id}\n"
        f"Teléfono: {telefono or 'No proporcionado'}\n\n"
        f"Resumen:\n{descripcion}"
    )

    try:
        async with httpx.AsyncClient() as client:
            uid = await _autenticar(client)
            if not uid:
                return None

            lead_id = await _execute_kw(client, uid, "crm.lead", "create", [{
                "name": titulo,
                "partner_name": nombre_cliente or "Sin nombre",
                "mobile": telefono or "",
                "description": cuerpo,
            }])

            log.info("Lead creado en Odoo CRM: id=%s canal=%s", lead_id, canal)
            return lead_id

    except Exception as e:
        log.error("Error creando lead en Odoo: %s", e)
        return None
