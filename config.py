import os

from dotenv import load_dotenv

load_dotenv()

# ─── META / INSTAGRAM ────────────────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        import logging
        logging.getLogger(__name__).critical("Variable de entorno requerida no configurada: %s", key)
    return val

VERIFY_TOKEN    = _require("META_VERIFY_TOKEN")
APP_SECRET      = _require("META_APP_SECRET")
IG_ACCESS_TOKEN = _require("IG_ACCESS_TOKEN")
IG_ACCOUNT_ID   = os.environ.get("IG_ACCOUNT_ID", "17841456843060136")

# ─── WHATSAPP ─────────────────────────────────────────────────────────────────
WA_ACCESS_TOKEN  = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_ID      = os.environ.get("WA_PHONE_ID", "")

# ─── ANTHROPIC / CLAUDE ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AUTO_RESPUESTA = os.environ.get("AUTO_RESPUESTA", "false").lower() == "true"
EXCLUIR_BOT    = {u.strip() for u in os.environ.get("EXCLUIR_BOT", "").split(",") if u.strip()}

SALUDO = os.environ.get(
    "SALUDO_BIENVENIDA",
    "¡Hola! Soy el asistente virtual de Clever CNC 👋 ¿En qué te puedo ayudar hoy?"
)

def _leer_archivo(nombre: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, "knowledge", nombre), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

_agente       = _leer_archivo("agente.txt")
_conocimiento = _leer_archivo("conocimiento.txt")

# Los precios se cargan dinámicamente desde precios.py (Google Doc o archivo local)
# SYSTEM_PROMPT se construye en runtime via get_system_prompt()
_PROMPT_BASE = os.environ.get("BOT_SYSTEM_PROMPT", "")


def get_system_prompt() -> str:
    from precios import obtener as obtener_precios
    base = _PROMPT_BASE or _agente
    if _conocimiento:
        base += f"\n\n## Información de la empresa:\n{_conocimiento}"
    precios = obtener_precios()
    if precios:
        base += f"\n\n## Lista de precios:\n{precios}"
    return base

# ─── ODOO CRM ─────────────────────────────────────────────────────────────────
ODOO_URL     = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
ODOO_DB      = os.environ.get("ODOO_DB", "")
ODOO_LOGIN    = os.environ.get("ODOO_LOGIN", "")

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
TURSO_URL   = os.environ.get("TURSO_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = os.environ.get("DB_PATH", "/app/bridgebot.db")
