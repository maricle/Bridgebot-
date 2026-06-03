import logging
import os

import httpx

log = logging.getLogger(__name__)

PRECIOS_DOC_URL = os.environ.get("PRECIOS_DOC_URL", "")
_contenido = ""


def _leer_local() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, "knowledge", "precios.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


async def cargar() -> str:
    global _contenido
    if PRECIOS_DOC_URL:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(PRECIOS_DOC_URL, timeout=15, follow_redirects=True)
                if resp.status_code == 200:
                    _contenido = resp.text.strip()
                    log.info("Precios cargados desde Google Doc (%d chars)", len(_contenido))
                    return _contenido
                log.warning("Google Doc devolvió %s — usando archivo local", resp.status_code)
        except Exception as e:
            log.warning("Error cargando precios desde Google Doc: %s — usando archivo local", e)

    _contenido = _leer_local()
    log.info("Precios cargados desde archivo local (%d chars)", len(_contenido))
    return _contenido


def obtener() -> str:
    return _contenido
