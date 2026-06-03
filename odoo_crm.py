import logging

import httpx

from config import ODOO_API_KEY, ODOO_DB, ODOO_URL

log = logging.getLogger(__name__)


async def crear_lead(nombre_cliente: str, telefono: str, descripcion: str,
                     canal: str = "instagram", user_id: str = "") -> int | None:
    if not ODOO_URL or not ODOO_API_KEY:
        log.warning("Odoo CRM no configurado — lead no creado")
        return None

    titulo = f"[{canal.upper()}] {nombre_cliente or 'Cliente sin nombre'}"
    cuerpo = (
        f"Canal: {canal}\n"
        f"ID usuario: {user_id}\n"
        f"Teléfono: {telefono or 'No proporcionado'}\n\n"
        f"Resumen de la consulta:\n{descripcion}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ODOO_URL}/web/dataset/call_kw",
                headers={
                    "Authorization": f"Bearer {ODOO_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "model": "crm.lead",
                        "method": "create",
                        "args": [{
                            "name": titulo,
                            "partner_name": nombre_cliente or "Sin nombre",
                            "mobile": telefono or "",
                            "description": cuerpo,
                        }],
                        "kwargs": {},
                    },
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                log.error("Odoo CRM error: %s", data["error"])
                return None
            lead_id = data.get("result")
            log.info("Lead creado en Odoo CRM: id=%s canal=%s", lead_id, canal)
            return lead_id

    except Exception as e:
        log.error("Error creando lead en Odoo: %s", e)
        return None
