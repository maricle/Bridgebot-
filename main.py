"""
BridgeBot — Instagram + WhatsApp → Claude AI Agent
Kleba Dev — 2026
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import dashboard_api
from routers import debug as debug_router
from routers import webhooks_meta
from routers import webhooks_odoo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import config as _config
    from config import ANTHROPIC_API_KEY
    from db import init_db
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
        inactivos = [uid for uid, lock in list(webhooks_meta._user_locks.items()) if not lock.locked()]
        for uid in inactivos:
            webhooks_meta._user_locks.pop(uid, None)
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
app.include_router(webhooks_meta.router)
app.include_router(dashboard_api.router)
