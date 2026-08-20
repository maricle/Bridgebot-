"""
BridgeBot — Instagram + WhatsApp → Claude AI Agent
Kleba Dev — 2026
"""

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import instagram
import whatsapp
from auth import verificar_api_key
from config import EXCLUIR_BOT, IG_ACCOUNT_ID, VERIFY_TOKEN
from db import (buscar_cliente_odoo_por_telefono,
                buscar_en_historial, buscar_usuario_por_telefono,
                conversacion_cerrada, contar_clientes_odoo,
                detectar_duplicados_telefono, es_usuario_nuevo,
                guardar_archivo, guardar_datos_cliente, guardar_mensaje, init_db,
                listar_archivos, listar_clientes_odoo, marcar_comprobante,
                limpiar_historial, marcar_mensaje_procesado, marcar_saludado,
                mensaje_ya_procesado, obtener_archivo_por_id, obtener_canonical_id,
                obtener_conversacion, obtener_conversaciones_recientes,
                obtener_datos_cliente, obtener_leads,
                obtener_usuarios, pausar_usuario, reanudar_usuario, resetear_cerrada,
                resetear_usuario, unificar_clientes, usuario_pausado)
from ai import generar_respuesta
from routers import debug as debug_router
from routers import webhooks_odoo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_user_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Imagen/sticker sueltos (sin pedido) → se guardan pero no se confirma por mensaje
_TIPOS_MEDIA_SIN_RESPUESTA = {"image", "sticker"}


async def _analizar_pdf_comprobante(media_id: str) -> dict | None:
    """Si el PDF recibido por WhatsApp tiene pinta de comprobante de pago,
    devuelve los datos extraídos por Claude. None si no aplica o algo falla —
    nunca debe frenar el flujo normal de recepción del archivo."""
    if not media_id:
        return None
    try:
        async with httpx.AsyncClient() as client:
            contenido = await whatsapp.descargar_media(client, media_id)
        if not contenido:
            return None
        from comprobantes import analizar_comprobante, extraer_texto_pdf
        texto = extraer_texto_pdf(contenido)
        if not texto:
            return None
        return await analizar_comprobante(texto)
    except Exception as e:
        log.error("Error analizando PDF como comprobante: %s", e)
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    import config as _config
    from config import ANTHROPIC_API_KEY
    from precios import cargar as cargar_precios
    await init_db()
    await _config.recargar_configuracion()
    await _config.recargar_conocimiento()
    await cargar_precios()
    t1 = asyncio.create_task(_refresh_precios_loop())
    t2 = asyncio.create_task(_sync_clientes_loop())
    t3 = asyncio.create_task(_sync_tareas_loop())
    modo = "AUTO_RESPUESTA" if _config.AUTO_RESPUESTA else "CLAUDE"
    log.info("BridgeBot v5 iniciado — modo: %s", modo)
    log.info("Claude configurado: %s", "SI" if ANTHROPIC_API_KEY else "NO")
    yield
    t1.cancel()
    t2.cancel()
    t3.cancel()


async def _refresh_precios_loop():
    from precios import cargar as cargar_precios
    while True:
        await asyncio.sleep(86400)
        await cargar_precios()
        log.info("Precios actualizados automáticamente")
        inactivos = [uid for uid, lock in list(_user_locks.items()) if not lock.locked()]
        for uid in inactivos:
            _user_locks.pop(uid, None)
        if inactivos:
            log.info("Limpieza locks usuarios: %d eliminados", len(inactivos))


async def _sync_clientes_loop():
    from odoo_crm import sincronizar_clientes
    from db import upsert_clientes_odoo
    await asyncio.sleep(60)  # esperar que la app arranque
    while True:
        clientes = await sincronizar_clientes()
        if clientes:
            await upsert_clientes_odoo(clientes)
        await asyncio.sleep(86400)  # repetir cada 24h


async def _sync_tareas_loop():
    from odoo_crm import sincronizar_tareas
    from db import upsert_tareas_odoo
    await asyncio.sleep(120)  # arrancar 2 min después del inicio
    while True:
        tareas = await sincronizar_tareas()
        if tareas:
            await upsert_tareas_odoo(tareas)
        await asyncio.sleep(1800)  # repetir cada 30 min


app = FastAPI(title="BridgeBot", version="5.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(debug_router.router)
app.include_router(webhooks_odoo.router)


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook")
async def recibir_webhook(request: Request):
    payload = await request.body()
    firma   = request.headers.get("X-Hub-Signature-256", "")
    if not instagram.verificar_firma(payload, firma):
        raise HTTPException(status_code=401, detail="Firma inválida")
    data   = await request.json()
    objeto = data.get("object", "")

    if objeto == "instagram":
        entry = data.get("entry", [{}])[0]
        log.info("IG webhook entry=%s", entry.get("id", "?"))
        asyncio.create_task(procesar_instagram(data))
    elif objeto == "whatsapp_business_account":
        log.info("WA webhook recibido via /webhook")
        asyncio.create_task(procesar_whatsapp(data))
    else:
        log.info("Webhook objeto desconocido: %s", objeto)

    return Response(status_code=200)


async def procesar_instagram(data: dict):
    try:
        # Archivos adjuntos
        sender_arch, archivos = instagram.extraer_archivos(data)
        if sender_arch and archivos:
            async with _user_locks[sender_arch]:
                canonical = await obtener_canonical_id(sender_arch)
                for arch in archivos:
                    archivo_id = await guardar_archivo(canonical, "instagram", arch["tipo"], url=arch.get("url", ""))
                    await guardar_mensaje(canonical, "user",
                        f"[Archivo recibido: {arch['tipo']}] /archivos/{archivo_id}/descargar")
                log.info("IG: %s archivo(s) guardado(s) para %s", len(archivos), sender_arch)
                if any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
                    async with httpx.AsyncClient() as client:
                        await instagram.enviar_mensaje(client, sender_arch, "¡Recibimos el archivo! Lo vamos a adjuntar al pedido.")
            return

        sender_id, mensaje = instagram.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("IG: evento sin texto, ignorando.")
            return
        if sender_id == IG_ACCOUNT_ID:
            log.info("IG: mensaje propio, ignorando.")
            return
        if sender_id in EXCLUIR_BOT:
            log.info("IG: atendido por humano %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return
        if await usuario_pausado(sender_id):
            log.info("IG: bot pausado para %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return

        # Deduplicación (antes del lock, después de filtros)
        message_id = instagram.extraer_message_id(data)
        if message_id:
            if await mensaje_ya_procesado(message_id):
                log.info("IG: mensaje duplicado ignorado: %s", message_id)
                return
            await marcar_mensaje_procesado(message_id)

        async with _user_locks[sender_id]:
            cerrada = await conversacion_cerrada(sender_id)
            if cerrada:
                await resetear_cerrada(sender_id)
                canonical = await obtener_canonical_id(sender_id)
                await limpiar_historial(canonical)
                log.info("IG: conversación cerrada reseteada para %s — procesando mensaje con Claude", sender_id)

            log.info("IG user=%s: %s", sender_id, mensaje[:100])
            async with httpx.AsyncClient() as client:
                from config import AUTO_RESPUESTA, SALUDO
                if AUTO_RESPUESTA:
                    canonical = await obtener_canonical_id(sender_id)
                    await guardar_mensaje(canonical, "user", mensaje)
                    if cerrada or await es_usuario_nuevo(sender_id):
                        await instagram.enviar_mensaje(client, sender_id, SALUDO)
                        if not cerrada:
                            await marcar_saludado(sender_id, "instagram")
                    return

                nuevo = await es_usuario_nuevo(sender_id)
                if nuevo:
                    await marcar_saludado(sender_id, "instagram")
                respuesta = await generar_respuesta(sender_id, mensaje, "instagram", es_nuevo=(cerrada or nuevo))
                if respuesta:
                    await instagram.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("IG error procesando evento: %s", e)


# ─── WHATSAPP ─────────────────────────────────────────────────────────────────

@app.get("/webhook/whatsapp")
async def verificar_webhook_wa(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook WA verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook/whatsapp")
async def recibir_whatsapp(request: Request):
    data = await request.json()
    log.info("WA webhook recibido: %s", str(data)[:200])
    asyncio.create_task(procesar_whatsapp(data))
    return Response(status_code=200)


async def procesar_whatsapp(data: dict):
    try:
        # Deduplicación: Meta reintenta el webhook si no recibe 200 a tiempo
        message_id = whatsapp.extraer_message_id(data)
        if message_id:
            if await mensaje_ya_procesado(message_id):
                log.info("WA: mensaje duplicado ignorado: %s", message_id)
                return
            await marcar_mensaje_procesado(message_id)

        # Archivos adjuntos
        sender_arch, archivos = whatsapp.extraer_archivos(data)
        if sender_arch and archivos:
            async with _user_locks[sender_arch]:
                canonical = await obtener_canonical_id(sender_arch)
                hay_comprobante = False
                for arch in archivos:
                    archivo_id = await guardar_archivo(canonical, "whatsapp", arch["tipo"], media_id=arch.get("media_id", ""))

                    datos_comprobante = None
                    if arch.get("mime_type") == "application/pdf":
                        datos_comprobante = await _analizar_pdf_comprobante(arch.get("media_id", ""))
                    documento_sin_clasificar = arch["tipo"] == "document" and not datos_comprobante

                    await guardar_mensaje(
                        canonical, "user",
                        f"[Archivo recibido: {arch['tipo']}] /archivos/{archivo_id}/descargar",
                        notificar_odoo=not datos_comprobante and not documento_sin_clasificar,
                    )

                    if datos_comprobante:
                        import json as _json
                        from odoo_crm import notificar_comprobante_pago
                        await marcar_comprobante(archivo_id, _json.dumps(datos_comprobante, ensure_ascii=False))
                        asyncio.create_task(notificar_comprobante_pago(canonical, datos_comprobante))
                        hay_comprobante = True
                    elif documento_sin_clasificar:
                        from odoo_crm import notificar_documento_recibido
                        asyncio.create_task(notificar_documento_recibido(canonical))

                log.info("WA: %s archivo(s) guardado(s) para %s", len(archivos), sender_arch)
                if hay_comprobante:
                    async with httpx.AsyncClient() as client:
                        await whatsapp.enviar_mensaje(client, sender_arch, "¡Recibimos tu comprobante de pago! 🙌 Ya quedó registrado.")
                elif any(arch["tipo"] not in _TIPOS_MEDIA_SIN_RESPUESTA for arch in archivos):
                    async with httpx.AsyncClient() as client:
                        await whatsapp.enviar_mensaje(client, sender_arch, "¡Recibimos el archivo! Lo vamos a adjuntar al pedido.")
            return

        sender_id, mensaje = whatsapp.extraer_mensaje(data)
        if not sender_id or not mensaje:
            log.info("WA: evento sin texto, ignorando.")
            return
        if sender_id in EXCLUIR_BOT:
            log.info("WA: atendido por humano %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return
        if await usuario_pausado(sender_id):
            log.info("WA: bot pausado para %s, ignorando.", sender_id)
            await guardar_mensaje(await obtener_canonical_id(sender_id), "user", mensaje)
            return

        async with _user_locks[sender_id]:
            cerrada = await conversacion_cerrada(sender_id)
            if cerrada:
                await resetear_cerrada(sender_id)
                canonical = await obtener_canonical_id(sender_id)
                await limpiar_historial(canonical)
                log.info("WA: conversación cerrada reseteada para %s — procesando mensaje con Claude", sender_id)

            log.info("WA user=%s: %s", sender_id, mensaje[:100])
            async with httpx.AsyncClient() as client:
                from config import AUTO_RESPUESTA, SALUDO
                if AUTO_RESPUESTA:
                    canonical = await obtener_canonical_id(sender_id)
                    await guardar_mensaje(canonical, "user", mensaje)
                    if cerrada or await es_usuario_nuevo(sender_id):
                        await whatsapp.enviar_mensaje(client, sender_id, SALUDO)
                        if not cerrada:
                            await marcar_saludado(sender_id, "whatsapp")
                    return

                nuevo = await es_usuario_nuevo(sender_id)
                if nuevo:
                    await marcar_saludado(sender_id, "whatsapp")
                    odoo_match = await buscar_cliente_odoo_por_telefono(sender_id)
                    if odoo_match and odoo_match.get("nombre"):
                        await guardar_datos_cliente(sender_id, nombre=odoo_match["nombre"],
                                                    email=odoo_match.get("email") or "")
                        log.info("WA: cliente Odoo identificado para %s (%s)", sender_id, odoo_match["nombre"])
                respuesta = await generar_respuesta(sender_id, mensaje, "whatsapp", es_nuevo=(cerrada or nuevo))
                if respuesta:
                    await whatsapp.enviar_mensaje(client, sender_id, respuesta)

    except Exception as e:
        log.exception("WA error procesando evento: %s", e)


# ─── UTILS ────────────────────────────────────────────────────────────────────


@app.get("/sync-tareas")
async def sync_tareas_manual():
    from odoo_crm import sincronizar_tareas
    from db import upsert_tareas_odoo
    tareas = await sincronizar_tareas()
    if tareas:
        await upsert_tareas_odoo(tareas)
    return {"ok": True, "tareas_sincronizadas": len(tareas)}


@app.get("/sync-clientes")
async def sync_clientes_manual():
    """Fuerza la sincronización de clientes de Odoo (misma que corre cada 24h)."""
    from odoo_crm import sincronizar_clientes
    from db import upsert_clientes_odoo
    clientes = await sincronizar_clientes()
    if clientes:
        await upsert_clientes_odoo(clientes)
    return {"ok": True, "clientes_sincronizados": len(clientes)}


@app.get("/clientes-odoo")
async def ver_clientes_odoo(q: str = "", limite: int = 50, offset: int = 0):
    """Lista de solo lectura de los clientes sincronizados desde Odoo."""
    clientes = await listar_clientes_odoo(q=q, limite=limite, offset=offset)
    total = await contar_clientes_odoo()
    return {"total": total, "clientes": clientes}


@app.get("/clientes/duplicados")
async def ver_duplicados_telefono():
    """Grupos de usuarios de WhatsApp con el mismo número (últimos 10 dígitos)
    guardado bajo ig_user_id distintos — candidatos a unificar."""
    return await detectar_duplicados_telefono()


@app.post("/clientes/unificar")
async def unificar_clientes_endpoint(request: Request):
    """Une un cliente duplicado al principal: mueve su historial y archivos,
    completa los datos que falten, y borra el duplicado."""
    await verificar_api_key(request)
    body = await request.json()
    primario = body.get("primario", "").strip()
    duplicado = body.get("duplicado", "").strip()
    if not primario or not duplicado:
        raise HTTPException(status_code=400, detail="primario y duplicado son requeridos")
    await unificar_clientes(primario, duplicado)
    return {"ok": True}


@app.get("/actualizar-precios")
async def actualizar_precios():
    from precios import cargar as cargar_precios, obtener
    await cargar_precios()
    contenido = obtener()
    return {
        "ok": True,
        "chars": len(contenido),
        "preview": contenido[:200] + "..." if len(contenido) > 200 else contenido,
    }


@app.get("/dashboard")
async def dashboard():
    import os
    base_dir = os.path.dirname(__file__)
    with open(os.path.join(base_dir, "static", "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    # Cache-busting: el navegador cachea agresivamente los estaticos servidos por
    # StaticFiles. Sin esto, despues de cada deploy los usuarios con la pestaña ya
    # abierta (o cache reciente) siguen viendo el dashboard.js/css viejo.
    for nombre in ("dashboard.css", "dashboard.js"):
        version = int(os.path.getmtime(os.path.join(base_dir, "static", nombre)))
        html = html.replace(f"/static/{nombre}", f"/static/{nombre}?v={version}")
    return HTMLResponse(html)


@app.get("/dashboard-config")
async def dashboard_config():
    from config import DASHBOARD_COLOR, NOMBRE_NEGOCIO
    return {"nombre": NOMBRE_NEGOCIO, "color": DASHBOARD_COLOR}


_CONFIG_CLAVES = {
    "SALUDO_BIENVENIDA", "AUTO_RESPUESTA", "ALIAS_TRANSFERENCIA",
    "NOMBRE_NEGOCIO", "DASHBOARD_COLOR",
}


@app.get("/config")
async def obtener_configuracion():
    from config import (ALIAS_TRANSFERENCIA, AUTO_RESPUESTA, DASHBOARD_COLOR,
                        NOMBRE_NEGOCIO, SALUDO)
    return {
        "SALUDO_BIENVENIDA": SALUDO,
        "AUTO_RESPUESTA": AUTO_RESPUESTA,
        "ALIAS_TRANSFERENCIA": ALIAS_TRANSFERENCIA,
        "NOMBRE_NEGOCIO": NOMBRE_NEGOCIO,
        "DASHBOARD_COLOR": DASHBOARD_COLOR,
    }


@app.post("/config")
async def guardar_configuracion(request: Request):
    await verificar_api_key(request)
    body = await request.json()
    from db import guardar_config
    for clave in _CONFIG_CLAVES:
        if clave in body:
            await guardar_config(f"config:{clave}", str(body[clave]))
    import config as _config
    await _config.recargar_configuracion()
    return {"ok": True}


@app.get("/config/knowledge")
async def obtener_knowledge():
    import config as _config
    return await _config.obtener_knowledge_efectivo()


@app.post("/config/knowledge/{archivo}")
async def guardar_knowledge(archivo: str, request: Request):
    import config as _config
    if archivo not in _config.KNOWLEDGE_ARCHIVOS:
        raise HTTPException(status_code=400, detail="Archivo no permitido")
    await verificar_api_key(request)
    body = await request.json()
    contenido = body.get("contenido", "")

    from db import guardar_config
    await guardar_config(f"knowledge:{archivo}", contenido)

    if archivo == "precios.md":
        from precios import cargar as cargar_precios
        await cargar_precios()
    else:
        await _config.recargar_conocimiento()
    return {"ok": True}


@app.get("/analytics")
async def analytics(desde: str = "", hasta: str = ""):
    from datetime import date, timedelta
    from analytics import obtener_analytics
    if not hasta:
        hasta = date.today().isoformat()
    if not desde:
        desde = (date.today() - timedelta(days=30)).isoformat()
    return await obtener_analytics(desde, hasta)


@app.get("/leads")
async def ver_leads():
    return await obtener_leads()


@app.get("/usuarios")
async def ver_usuarios():
    return await obtener_usuarios()


@app.get("/conversacion/{user_id}")
async def ver_conversacion(user_id: str):
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


@app.get("/buscar-contenido")
async def buscar_por_contenido(q: str):
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Texto de búsqueda muy corto")
    resultados = await buscar_en_historial(q.strip())
    return resultados


@app.get("/historial-reciente")
async def historial_reciente(limite: int = 20, offset: int = 0, canal: str = ""):
    return await obtener_conversaciones_recientes(limite=limite, offset=offset, canal=canal)


@app.get("/buscar")
async def buscar_por_telefono(telefono: str):
    user_id = await buscar_usuario_por_telefono(telefono)
    if not user_id:
        return {"encontrado": False, "user_id": None, "cliente": {}, "historial": []}
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"encontrado": True, "user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


@app.post("/responder")
async def responder_whatsapp(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    mensaje = body.get("mensaje", "").strip()
    canal = body.get("canal", "").strip()
    if not user_id or not mensaje:
        raise HTTPException(status_code=400, detail="user_id y mensaje son requeridos")

    if not canal:
        datos = await obtener_datos_cliente(user_id)
        canal = datos.get("canal", "")

    async with httpx.AsyncClient() as client:
        if canal == "instagram":
            ok = await instagram.enviar_mensaje(client, user_id, mensaje)
        else:
            ok = await whatsapp.enviar_mensaje(client, user_id, mensaje)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Error enviando mensaje por {canal or 'WhatsApp'}")
    canonical = await obtener_canonical_id(user_id)
    await guardar_mensaje(canonical, "assistant", mensaje)
    return {"ok": True}


@app.get("/archivos")
async def ver_archivos():
    return await listar_archivos()


@app.get("/archivos/{archivo_id}/descargar")
async def descargar_archivo(archivo_id: int):
    from fastapi.responses import StreamingResponse
    from config import WA_ACCESS_TOKEN
    archivo = await obtener_archivo_por_id(archivo_id)
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    media_id = archivo.get("media_id", "")
    url_directa = archivo.get("url", "")

    headers_meta = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}

    async with httpx.AsyncClient() as client:
        # WA: refrescar URL via media_id
        if media_id and not url_directa:
            info = await client.get(
                f"https://graph.facebook.com/v19.0/{media_id}",
                headers=headers_meta, timeout=10,
            )
            if info.status_code != 200:
                raise HTTPException(status_code=502, detail="No se pudo obtener la URL del archivo de Meta")
            url_directa = info.json().get("url", "")

        if not url_directa:
            raise HTTPException(status_code=404, detail="Sin URL disponible para este archivo")

        resp = await client.get(url_directa, headers=headers_meta, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Error descargando el archivo de Meta")

        content_type = resp.headers.get("content-type", "application/octet-stream")
        tipo = archivo.get("tipo", "archivo")
        ext = content_type.split("/")[-1].split(";")[0]
        filename = f"{tipo}_{archivo_id}.{ext}"

        return StreamingResponse(
            iter([resp.content]),
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@app.delete("/usuario/{user_id}")
async def borrar_usuario(user_id: str):
    await resetear_usuario(user_id)
    return {"ok": True, "mensaje": f"Usuario {user_id} reseteado"}


@app.post("/usuario/{user_id}/pausar")
async def pausar_usuario_endpoint(user_id: str):
    await pausar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot pausado para {user_id}"}


@app.post("/usuario/{user_id}/reanudar")
async def reanudar_usuario_endpoint(user_id: str):
    await reanudar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot reanudado para {user_id}"}
