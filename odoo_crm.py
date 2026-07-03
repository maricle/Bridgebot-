import base64
import logging
import re

import httpx

from config import (BOT_NOMBRE, MODO_DEV, ODOO_API_KEY, ODOO_DB, ODOO_DESTINOS,
                    ODOO_LOGIN, ODOO_NOTIFICAR_USUARIOS, ODOO_URL)

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


_URL_RE = re.compile(r'https?://[^\s<>"\']+')


def _extraer_links(historial: list) -> list[str]:
    vistos: set[str] = set()
    links: list[str] = []
    for m in historial:
        if m.get("role") != "user":
            continue
        for url in _URL_RE.findall(m.get("content", "")):
            if url not in vistos:
                vistos.add(url)
                links.append(url)
    return links


def _transcripcion_html(historial: list) -> str:
    if not historial:
        return ""
    import html as _html
    partes = ["<hr/><p><b>TRANSCRIPCIÓN DEL CHAT</b></p>"]
    for m in historial:
        label = "<b>Cliente:</b>" if m["role"] == "user" else f"<b>{BOT_NOMBRE}:</b>"
        texto = _html.escape(m["content"]).replace("\n", "<br/>")
        partes.append(f"<p>{label} {texto}</p>")
    return "".join(partes)



async def _adjuntar_archivo(client: httpx.AsyncClient, uid: int, lead_id: int,
                             arch: dict, canal: str):
    """Descarga el archivo de Meta y lo adjunta al lead en Odoo."""
    try:
        content = None
        filename = f"adjunto_{arch.get('media_id') or 'ig'}"

        _TIPO_EXT = {"image": "jpg", "video": "mp4", "audio": "m4a",
                     "document": "pdf", "sticker": "webp"}

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
                ext = _TIPO_EXT.get(tipo, tipo)
                filename = f"{filename}.{ext}"

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


async def sincronizar_tareas() -> list[dict]:
    """
    Trae project.task de Odoo desde el 2026-01-01 con órdenes confirmadas.
    El nro de orden se extrae del nombre con el patrón '2026-09374 - CLIENTE'.

    Ruta para obtener el cliente real:
      project.task → sale_order_id → sale.order.partner_id → res.partner
    (el partner_id del task suele estar vacío o ser un contacto secundario)
    """
    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        return []
    try:
        async with httpx.AsyncClient() as client:
            uid = await _autenticar(client)
            if not uid:
                return []

            # Paso 1a: tareas con orden de venta desde 2026 (sin filtrar por estado aún)
            tasks_raw = await _execute_kw(
                client, uid, "project.task", "search_read",
                [[["sale_order_id", "!=", False],
                  ["create_date", ">=", "2026-01-01"]]],
                {"fields": ["id", "name", "stage_id", "sale_order_id"], "limit": 1000},
            )
            if not tasks_raw:
                log.info("sincronizar_tareas: sin tareas en Odoo desde 2026-01-01")
                return []

            log.info("sincronizar_tareas: %d tareas encontradas antes de filtrar estado", len(tasks_raw))

            # Paso 1b: traer sale.orders y filtrar solo confirmadas (sale/done) en Python
            sale_order_ids = list({
                t["sale_order_id"][0]
                for t in tasks_raw
                if isinstance(t.get("sale_order_id"), list)
            })
            so_data = await _execute_kw(
                client, uid, "sale.order", "search_read",
                [[["id", "in", sale_order_ids]]],
                {"fields": ["id", "partner_id", "state"]},
            )
            sale_orders: dict[int, dict] = {
                so["id"]: so for so in so_data
                if so.get("state") in ("sale", "done")
            }
            log.info("sincronizar_tareas: %d órdenes confirmadas (de %d con tarea)", len(sale_orders), len(sale_order_ids))

            # Solo mantener tareas con órdenes confirmadas
            tasks_raw = [
                t for t in tasks_raw
                if isinstance(t.get("sale_order_id"), list)
                and t["sale_order_id"][0] in sale_orders
            ]
            if not tasks_raw:
                log.info("sincronizar_tareas: sin tareas con órdenes confirmadas")
                return []

            # Paso 2: verificar que las sale.orders traen partner_id
            so_sin_partner = [so["id"] for so in sale_orders.values() if not isinstance(so.get("partner_id"), list)]
            if so_sin_partner:
                log.warning("sincronizar_tareas: %d órdenes sin partner_id: %s", len(so_sin_partner), so_sin_partner[:5])

            # Paso 3: traer teléfono, nombre y documento del partner
            partner_ids = list({
                so["partner_id"][0]
                for so in sale_orders.values()
                if isinstance(so.get("partner_id"), list)
            })
            log.info("sincronizar_tareas: buscando %d partners únicos", len(partner_ids))

            partner_data = await _execute_kw(
                client, uid, "res.partner", "search_read",
                [[["id", "in", partner_ids]]],
                {"fields": ["id", "name", "phone", "vat"]},
            )
            partners: dict[int, dict] = {p["id"]: p for p in partner_data}

            # Log de muestra para verificar datos del partner
            muestra = [
                {"id": p["id"], "name": p.get("name"), "phone": p.get("phone")}
                for p in partner_data[:3]
            ]
            log.info("sincronizar_tareas: muestra de partners obtenidos: %s", muestra)

        tareas = []
        for t in tasks_raw:
            sale_ref    = t.get("sale_order_id")
            so_id       = sale_ref[0] if isinstance(sale_ref, list) else 0
            sale_name   = sale_ref[1] if isinstance(sale_ref, list) else ""
            so          = sale_orders.get(so_id, {})
            p_ref       = so.get("partner_id")
            partner     = partners.get(p_ref[0] if isinstance(p_ref, list) else 0, {})

            telefono = "".join(
                c for c in (partner.get("phone") or "") if c.isdigit()
            )
            stage_name = t["stage_id"][1] if isinstance(t.get("stage_id"), list) else ""

            m = re.match(r"^\d{4}-(\d+)", t["name"])
            nro_orden = m.group(1) if m else ""

            tareas.append({
                "odoo_id":         t["id"],
                "task_name":       t["name"],
                "nro_orden":       nro_orden,
                "stage":           stage_name,
                "partner_name":    partner.get("name") or "",
                "telefono":        telefono,
                "documento":       partner.get("vat") or "",
                "sale_order_name": sale_name,
            })

        sin_tel = sum(1 for t in tareas if not t["telefono"])
        log.info("Odoo sync tareas: %d obtenidas, %d sin teléfono", len(tareas), sin_tel)
        return tareas
    except Exception as e:
        log.error("Error sincronizando tareas de Odoo: %s", e)
        return []


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
                [[["phone", "!=", False], ["active", "=", True]]],
                {"fields": ["id", "name", "phone", "email"], "limit": 5000},
            )
        clientes = []
        for p in partners:
            tel = p.get("phone") or ""
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


async def actualizar_partner(odoo_id: int, email: str = "") -> bool:
    """Actualiza email de un res.partner en Odoo."""
    if MODO_DEV:
        log.info("MODO_DEV activo — partner Odoo %s NO actualizado (simulado)", odoo_id)
        return False

    if not odoo_id or not email or not ODOO_URL:
        return False
    try:
        async with httpx.AsyncClient() as client:
            uid = await _autenticar(client)
            if not uid:
                return False
            await _execute_kw(client, uid, "res.partner", "write",
                              [[odoo_id], {"email": email}])
            log.info("Partner Odoo %s — email actualizado", odoo_id)
            return True
    except Exception as e:
        log.error("Error actualizando partner Odoo %s: %s", odoo_id, e)
        return False


def _resolver_destino(destino: str) -> tuple[int | None, int | None]:
    """Retorna (company_id, responsable_id) según el destino (área/servicio) configurado."""
    raw = ODOO_DESTINOS.get(destino) or ODOO_DESTINOS.get("default")
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
                     destino: str = "default",
                     email: str = "",
                     requiere_diseno: bool = False,
                     partner_id: int | None = None) -> int | None:
    if MODO_DEV:
        log.info("MODO_DEV activo — lead NO creado en Odoo (simulado): %s / %s", nombre_cliente, telefono)
        return None

    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        log.warning("Odoo CRM no configurado — lead no creado")
        return None

    company_id, responsable_id = _resolver_destino(destino)

    import html as _html
    titulo = f"[{canal.upper()}][{destino.upper()}] {nombre_cliente or 'Cliente sin nombre'}"
    links = _extraer_links(historial or [])
    links_html = ""
    if links:
        items = "".join(f'<li><a href="{_html.escape(u)}">{_html.escape(u)}</a></li>' for u in links)
        links_html = f"<hr/><p><b>━━━ LINKS ENVIADOS POR EL CLIENTE ━━━</b></p><ul>{items}</ul>"
    cuerpo = (
        f"<p><b>Canal:</b> {canal} &nbsp;|&nbsp; <b>Área:</b> {destino}</p>"
        f"<p><b>Teléfono:</b> {_html.escape(telefono or 'No proporcionado')}</p>"
        f"<p>{_html.escape(descripcion)}</p>"
        + links_html
        + _transcripcion_html(historial or [])
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
                "email_from": email or "",
                "description": cuerpo,
                "user_id": responsable_id,
            }
            if company_id:
                vals["company_id"] = company_id
            if partner_id:
                vals["partner_id"] = partner_id

            lead_id = await _execute_kw(client, uid, "crm.lead", "create", [vals])
            log.info("Lead creado en Odoo CRM: id=%s canal=%s destino=%s", lead_id, canal, destino)

            # Agregar etiqueta "Diseño" si el cliente pidió diseño
            if requiere_diseno:
                try:
                    tag_ids = await _execute_kw(
                        client, uid, "crm.tag", "search", [[["name", "=", "Diseño"]]]
                    )
                    tag_id = tag_ids[0] if tag_ids else await _execute_kw(
                        client, uid, "crm.tag", "create", [{"name": "Diseño"}]
                    )
                    await _execute_kw(client, uid, "crm.lead", "write",
                                      [[lead_id], {"tag_ids": [(4, tag_id)]}])
                    log.info("Etiqueta Diseño agregada al lead %s", lead_id)
                except Exception as e:
                    log.warning("No se pudo agregar etiqueta Diseño: %s", e)

            # Suscribir usuarios adicionales al lead
            if ODOO_NOTIFICAR_USUARIOS:
                try:
                    partner_ids = await _execute_kw(
                        client, uid, "res.users", "read",
                        [ODOO_NOTIFICAR_USUARIOS], {"fields": ["partner_id"]},
                    )
                    pids = [p["partner_id"][0] for p in partner_ids if p.get("partner_id")]
                    if pids:
                        await _execute_kw(client, uid, "crm.lead", "message_subscribe",
                                          [[lead_id]], {"partner_ids": pids})
                except Exception as e:
                    log.warning("No se pudieron suscribir usuarios adicionales: %s", e)

            for arch in (archivos or []):
                await _adjuntar_archivo(client, uid, lead_id, arch, canal)

            return lead_id

    except Exception as e:
        log.error("Error creando lead en Odoo: %s", e)
        return None
