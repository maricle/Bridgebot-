from fastapi import HTTPException, Request

from config import BRIDGE_API_KEY


async def verificar_api_key(request: Request):
    """Valida la API key para endpoints protegidos (config, webhooks de Odoo, etc.).

    Header X-Api-Key (integraciones normales) o ?key=... en la URL (la acción
    "Webhook" nativa de Odoo no permite configurar headers custom). Si
    BRIDGE_API_KEY no está configurada, se omite la validación (modo dev)."""
    if not BRIDGE_API_KEY:
        return
    api_key = request.headers.get("X-Api-Key", "") or request.query_params.get("key", "")
    if api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")
