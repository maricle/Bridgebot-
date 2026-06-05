import base64
import logging

import httpx

from config import ODOO_API_KEY, ODOO_DB, ODOO_LOGIN, ODOO_URL

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


def _formatear_transcripcion(historial: list) -> str:
    if not historial:
        return ""
    lineas = ["\n\n--- TRANSCRIPCIÓN DEL CHAT ---"]
    for m in historial:
        rol = "Cliente" if m["role"] == "user" else "Bot"
        lineas.append(f"{rol}: {m['content']}")
    return "\n".join(lineas)


async def _adjuntar_archivo(client: httpx.AsyncClient, uid: int, lead_id: int,
                             arch: dict, canal: str):
    """Descarga el archivo de Meta y lo adjunta al lead en Odoo."""
    try:
        content = None
        filename = f"adjunto_{arch.get('media_id') or 'ig'}"

        if canal == "whatsapp" and arch.get("media_id"):
            from config import WA_ACCESS_TOKEN
            meta = await client.get(
                f"https://graph.facebook.com/v19.0/{arch['media_id']}",
                headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"},
                timeout=15,
            )
            if meta.status_code == 200:
                download_url = meta.json().get("url", "")
                mime = meta.json().get("mime_type", "application/octet-stream")
                ext = mime.split("/")[-1].split(";")[0]
                filename = f"{filename}.{ext}"
                dl = await client.get(download_url, headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}, timeout=30)
                if dl.status_code == 200:
                    content = dl.content

        elif arch.get("url"):
            from config import IG_ACCESS_TOKEN
            dl = await client.get(arch["url"], headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"}, timeout=30)
            if dl.status_code == 200:
                content = dl.content
                tipo = arch.get("tipo", "file")
                filename = f"{filename}.{tipo}"

        if content:
            await _execute_kw(client, uid, "ir.attachment", "create", [{
                "name": filename,
                "datas": base64.b64encode(content).decode(),
                "res_model": "crm.lead",
                "res_id": lead_id,
            }])
            log.info("Archivo adjuntado al lead %s: %s", lead_id, filename)
    except Exception as e:
        log.error("Error adjuntando archivo al lead %s: %s", lead_id, e)


async def crear_lead(nombre_cliente: str, telefono: str, descripcion: str,
                     canal: str = "instagram", user_id: str = "",
                     historial: list | None = None,
                     archivos: list | None = None) -> int | None:
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        log.warning("Odoo CRM no configurado — lead no creado")
        return None

    titulo = f"[{canal.upper()}] {nombre_cliente or 'Cliente sin nombre'}"
    cuerpo = (
        f"Canal: {canal}\n"
        f"ID usuario: {user_id}\n"
        f"Teléfono: {telefono or 'No proporcionado'}\n\n"
        f"Resumen:\n{descripcion}"
        + _formatear_transcripcion(historial or [])
    )

    try:
        async with httpx.AsyncClient() as client:
            uid = await _autenticar(client)
            if not uid:
                return None

            lead_id = await _execute_kw(client, uid, "crm.lead", "create", [{
                "name": titulo,
                "partner_name": nombre_cliente or "Sin nombre",
                "phone": telefono or "",
                "description": cuerpo,
                "user_id": 1,
            }])

            log.info("Lead creado en Odoo CRM: id=%s canal=%s", lead_id, canal)

            for arch in (archivos or []):
                await _adjuntar_archivo(client, uid, lead_id, arch, canal)

            return lead_id

    except Exception as e:
        log.error("Error creando lead en Odoo: %s", e)
        return None
