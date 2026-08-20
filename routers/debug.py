"""Endpoints de diagnóstico — probar la conexión con Claude/Odoo y el estado general."""

import httpx
from fastapi import APIRouter

from db import stats

router = APIRouter()


@router.get("/test-claude")
async def test_claude():
    from config import ANTHROPIC_API_KEY
    from ai import _llamar_claude
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "ANTHROPIC_API_KEY no configurada"}
    respuesta = await _llamar_claude(
        messages=[{"role": "user", "content": "Respondé solo: hola"}],
        max_tokens=50,
    )
    if respuesta:
        return {"ok": True, "respuesta": respuesta}
    return {"ok": False, "error": "Claude no respondió — revisá los logs"}


@router.get("/test-odoo")
async def test_odoo():
    from config import ODOO_API_KEY, ODOO_URL, ODOO_LOGIN

    if not ODOO_URL or not ODOO_API_KEY or not ODOO_LOGIN:
        return {
            "ok": False,
            "error": "Variables faltantes",
            "ODOO_URL": ODOO_URL or "VACÍO",
            "ODOO_API_KEY": f"{ODOO_API_KEY[:6]}..." if ODOO_API_KEY else "VACÍO",
            "ODOO_LOGIN": ODOO_LOGIN or "VACÍO",
        }

    from odoo_crm import crear_lead
    lead_id = await crear_lead(
        nombre_cliente="Test BridgeBot",
        telefono="0000000000",
        descripcion="Lead de prueba — podés eliminarlo.",
        canal="test",
        user_id="test",
    )
    if lead_id:
        return {"ok": True, "odoo_lead_id": lead_id}
    return {"ok": False, "mensaje": "Revisá los logs de Railway para ver el error exacto"}


@router.get("/health")
async def health():
    from config import AUTO_RESPUESTA, WA_ACCESS_TOKEN, WA_PHONE_ID
    wa_ok = False
    wa_numero = None
    if WA_ACCESS_TOKEN and WA_PHONE_ID:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}",
                    params={"access_token": WA_ACCESS_TOKEN},
                    timeout=8,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    wa_ok = True
                    wa_numero = data.get("display_phone_number")
        except Exception:
            pass
    return {
        "status": "ok",
        "version": "5.0.0",
        "modo": "AUTO_RESPUESTA" if AUTO_RESPUESTA else "CLAUDE",
        "whatsapp": {"ok": wa_ok, "numero": wa_numero},
        **(await stats()),
    }
