"""API que consume el dashboard (/dashboard): configuración, analytics,
conversaciones, archivos, clientes sincronizados y las acciones de
sincronización manual con Odoo."""

import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

import instagram
import odoo_crm
import whatsapp
from auth import verificar_api_key
from config import WA_ACCESS_TOKEN
from db import (buscar_en_historial, buscar_usuario_por_telefono,
                contar_clientes_odoo, detectar_duplicados_telefono,
                guardar_config, guardar_mensaje, listar_archivos,
                listar_clientes_odoo, obtener_archivo_por_id, obtener_canonical_id,
                obtener_conversacion, obtener_conversaciones_recientes,
                obtener_datos_cliente, obtener_leads, obtener_usuarios,
                pausar_usuario, reanudar_usuario, resetear_usuario,
                unificar_clientes, upsert_clientes_odoo, upsert_tareas_odoo,
                usuario_pausado)

router = APIRouter()


@router.get("/sync-tareas")
async def sync_tareas_manual():
    tareas = await odoo_crm.sincronizar_tareas()
    if tareas:
        await upsert_tareas_odoo(tareas)
    return {"ok": True, "tareas_sincronizadas": len(tareas)}


@router.get("/sync-clientes")
async def sync_clientes_manual():
    """Fuerza la sincronización de clientes de Odoo (misma que corre cada 24h)."""
    clientes = await odoo_crm.sincronizar_clientes()
    if clientes:
        await upsert_clientes_odoo(clientes)
    return {"ok": True, "clientes_sincronizados": len(clientes)}


@router.get("/clientes-odoo")
async def ver_clientes_odoo(q: str = "", limite: int = 50, offset: int = 0):
    """Lista de solo lectura de los clientes sincronizados desde Odoo."""
    clientes = await listar_clientes_odoo(q=q, limite=limite, offset=offset)
    total = await contar_clientes_odoo()
    return {"total": total, "clientes": clientes}


@router.get("/clientes/duplicados")
async def ver_duplicados_telefono():
    """Grupos de usuarios de WhatsApp con el mismo número (últimos 10 dígitos)
    guardado bajo ig_user_id distintos — candidatos a unificar."""
    return await detectar_duplicados_telefono()


@router.post("/clientes/unificar")
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


@router.get("/actualizar-precios")
async def actualizar_precios():
    from precios import cargar as cargar_precios, obtener
    await cargar_precios()
    contenido = obtener()
    return {
        "ok": True,
        "chars": len(contenido),
        "preview": contenido[:200] + "..." if len(contenido) > 200 else contenido,
    }


@router.get("/dashboard")
async def dashboard():
    base_dir = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(base_dir, "static", "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    # Cache-busting: el navegador cachea agresivamente los estaticos servidos por
    # StaticFiles. Sin esto, despues de cada deploy los usuarios con la pestaña ya
    # abierta (o cache reciente) siguen viendo el dashboard.js/css viejo.
    for nombre in ("dashboard.css", "dashboard.js"):
        version = int(os.path.getmtime(os.path.join(base_dir, "static", nombre)))
        html = html.replace(f"/static/{nombre}", f"/static/{nombre}?v={version}")
    return HTMLResponse(html)


@router.get("/dashboard-config")
async def dashboard_config():
    from config import DASHBOARD_COLOR, NOMBRE_NEGOCIO
    return {"nombre": NOMBRE_NEGOCIO, "color": DASHBOARD_COLOR}


_CONFIG_CLAVES = {
    "SALUDO_BIENVENIDA", "AUTO_RESPUESTA", "ALIAS_TRANSFERENCIA",
    "NOMBRE_NEGOCIO", "DASHBOARD_COLOR",
}


@router.get("/config")
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


@router.post("/config")
async def guardar_configuracion(request: Request):
    await verificar_api_key(request)
    body = await request.json()
    for clave in _CONFIG_CLAVES:
        if clave in body:
            await guardar_config(f"config:{clave}", str(body[clave]))
    import config as _config
    await _config.recargar_configuracion()
    return {"ok": True}


@router.get("/config/knowledge")
async def obtener_knowledge():
    import config as _config
    return await _config.obtener_knowledge_efectivo()


@router.post("/config/knowledge/{archivo}")
async def guardar_knowledge(archivo: str, request: Request):
    import config as _config
    if archivo not in _config.KNOWLEDGE_ARCHIVOS:
        raise HTTPException(status_code=400, detail="Archivo no permitido")
    await verificar_api_key(request)
    body = await request.json()
    contenido = body.get("contenido", "")

    await guardar_config(f"knowledge:{archivo}", contenido)

    if archivo == "precios.md":
        from precios import cargar as cargar_precios
        await cargar_precios()
    else:
        await _config.recargar_conocimiento()
    return {"ok": True}


@router.get("/analytics")
async def analytics(desde: str = "", hasta: str = ""):
    from datetime import date, timedelta
    from analytics import obtener_analytics
    if not hasta:
        hasta = date.today().isoformat()
    if not desde:
        desde = (date.today() - timedelta(days=30)).isoformat()
    return await obtener_analytics(desde, hasta)


@router.get("/leads")
async def ver_leads():
    return await obtener_leads()


@router.get("/usuarios")
async def ver_usuarios():
    return await obtener_usuarios()


@router.get("/conversacion/{user_id}")
async def ver_conversacion(user_id: str):
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


@router.get("/buscar-contenido")
async def buscar_por_contenido(q: str):
    if not q or len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Texto de búsqueda muy corto")
    resultados = await buscar_en_historial(q.strip())
    return resultados


@router.get("/historial-reciente")
async def historial_reciente(limite: int = 20, offset: int = 0, canal: str = ""):
    return await obtener_conversaciones_recientes(limite=limite, offset=offset, canal=canal)


@router.get("/buscar")
async def buscar_por_telefono(telefono: str):
    user_id = await buscar_usuario_por_telefono(telefono)
    if not user_id:
        return {"encontrado": False, "user_id": None, "cliente": {}, "historial": []}
    datos = await obtener_datos_cliente(user_id)
    historial = await obtener_conversacion(user_id)
    pausado = await usuario_pausado(user_id)
    return {"encontrado": True, "user_id": user_id, "cliente": datos, "historial": historial, "pausado": pausado}


@router.post("/responder")
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


@router.get("/archivos")
async def ver_archivos():
    return await listar_archivos()


@router.get("/archivos/{archivo_id}/descargar")
async def descargar_archivo(archivo_id: int):
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


@router.delete("/usuario/{user_id}")
async def borrar_usuario(user_id: str):
    await resetear_usuario(user_id)
    return {"ok": True, "mensaje": f"Usuario {user_id} reseteado"}


@router.post("/usuario/{user_id}/pausar")
async def pausar_usuario_endpoint(user_id: str):
    await pausar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot pausado para {user_id}"}


@router.post("/usuario/{user_id}/reanudar")
async def reanudar_usuario_endpoint(user_id: str):
    await reanudar_usuario(user_id)
    return {"ok": True, "mensaje": f"Bot reanudado para {user_id}"}
