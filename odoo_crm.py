import base64
import logging

import httpx

from config import (ODOO_API_KEY, ODOO_DB, ODOO_DESTINO_CARTELERIA,
                    ODOO_DESTINO_OFICINA, ODOO_LOGIN, ODOO_URL)

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
    sep = "\n" + "─" * 45 + "\n"
    bloques = ["\n\n━━━ TRANSCRIPCIÓN DEL CHAT ━━━"]
    for m in historial:
        if m["role"] == "user":
            bloques.append(f"CLIENTE:\n{m['content']}")
        else:
            bloques.append(f"BOT:\n{m['content']}")
    return sep.join(bloques)


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


async def sincronizar_clientes() -> list[dict]:
    """Trae todos los res.partner con teléfono de Odoo para sync nocturno."""
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        return []
    try:
        async with httpx.AsyncClient() as client:
            uid = await _autenticar(client)
            if not uid:
                return []
            partners = await _execute_kw(
                client, uid, "res.partner", "search_read",
                [[["phone", "!=", False]]],
                {"fields": ["id", "name", "phone", "email"], "limit": 5000},
            )
        clientes = []
        for p in partners:
            tel = p.get("phone") or p.get("mobile") or ""
            digitos = "".join(c for c in tel if c.isdigit())
            if len(digitos) >= 7:
                clientes.append({
                    "odoo_id":  p["id"],
                    "nombre":   p.get("name") or "",
                    "telefono": digitos,
                    "email":    p.get("email") or "",
                })
        log.info("Odoo sync: %d partners con teléfono", len(clientes))
        return clientes
    except Exception as e:
        log.error("Error sincronizando clientes de Odoo: %s", e)
        return []


def _resolver_destino(destino: str) -> tuple[int | None, int | None]:
    """Retorna (company_id, responsable_id) según el destino configurado."""
    raw = ODOO_DESTINO_CARTELERIA if destino == "carteleria" else ODOO_DESTINO_OFICINA
    if not raw:
        return None, 1
    partes = raw.split(":")
    company_id    = int(partes[0]) if len(partes) > 0 and partes[0].isdigit() else None
    responsable   = int(partes[1]) if len(partes) > 1 and partes[1].isdigit() else 1
    return company_id, responsable


async def crear_lead(nombre_cliente: str, telefono: str, descripcion: str,
                     canal: str = "instagram", user_id: str = "",
                     historial: list | None = None,
                     archivos: list | None = None,
                     destino: str = "oficina") -> int | None:
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        log.warning("Odoo CRM no configurado — lead no creado")
        return None

    company_id, responsable_id = _resolver_destino(destino)

    titulo = f"[{canal.upper()}][{destino.upper()}] {nombre_cliente or 'Cliente sin nombre'}"
    cuerpo = (
        f"Canal: {canal}\n"
        f"Área: {destino}\n"
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

            vals: dict = {
                "name": titulo,
                "partner_name": nombre_cliente or "Sin nombre",
                "phone": telefono or "",
                "description": cuerpo,
                "user_id": responsable_id,
            }
            if company_id:
                vals["company_id"] = company_id

            lead_id = await _execute_kw(client, uid, "crm.lead", "create", [vals])

            log.info("Lead creado en Odoo CRM: id=%s canal=%s destino=%s", lead_id, canal, destino)

            for arch in (archivos or []):
                await _adjuntar_archivo(client, uid, lead_id, arch, canal)

            return lead_id

    except Exception as e:
        log.error("Error creando lead en Odoo: %s", e)
        return None
