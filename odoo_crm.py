import logging

import httpx

from config import ODOO_API_KEY, ODOO_DB, ODOO_URL, ODOO_USER

log = logging.getLogger(__name__)


async def _get_session(client: httpx.AsyncClient) -> str | None:
    """Autentica con API key y devuelve el session_id."""
    try:
        resp = await client.post(
            f"{ODOO_URL}/web/session/authenticate",
            json={
                "jsonrpc": "2.0",
                "method": "call",
                "params": {
                    "db": ODOO_DB,
                    "login": ODOO_USER,
                    "password": ODOO_API_KEY,
                },
            },
            timeout=15,
        )
        data = resp.json()
        if "error" in data:
            log.error("Odoo auth error: %s", data["error"])
            return None
        session_id = resp.cookies.get("session_id")
        if not session_id:
            # Algunos setups lo devuelven en el body
            session_id = data.get("result", {}).get("session_id")
        return session_id
    except Exception as e:
        log.error("Error autenticando en Odoo: %s", e)
        return None


async def _rpc(client: httpx.AsyncClient, session_id: str, model: str,
               method: str, args: list, kwargs: dict = {}) -> any:
    resp = await client.post(
        f"{ODOO_URL}/web/dataset/call_kw",
        cookies={"session_id": session_id},
        json={
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs},
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise Exception(f"Odoo RPC error: {data['error']}")
    return data["result"]


async def crear_lead(nombre_cliente: str, telefono: str, descripcion: str,
                     canal: str = "instagram", user_id: str = "") -> int | None:
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_USER:
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
            session_id = await _get_session(client)
            if not session_id:
                log.error("No se pudo obtener sesión de Odoo")
                return None

            lead_id = await _rpc(client, session_id, "crm.lead", "create", [{
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
