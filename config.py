import os

from dotenv import load_dotenv

load_dotenv()

# ─── META / INSTAGRAM ────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.environ["META_VERIFY_TOKEN"]
APP_SECRET      = os.environ["META_APP_SECRET"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID   = os.environ.get("IG_ACCOUNT_ID", "17841456843060136")

# ─── WHATSAPP ─────────────────────────────────────────────────────────────────
WA_ACCESS_TOKEN  = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_ID      = os.environ.get("WA_PHONE_ID", "")

# ─── GROQ AI ──────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
AUTO_RESPUESTA = os.environ.get("AUTO_RESPUESTA", "false").lower() == "true"

SALUDO = os.environ.get(
    "SALUDO_BIENVENIDA",
    "¡Hola! Soy el asistente virtual de Clever CNC 👋 ¿En qué te puedo ayudar hoy?"
)

def _leer_archivo(nombre: str) -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base_dir, nombre), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

_agente      = _leer_archivo("agente.txt")
_conocimiento = _leer_archivo("conocimiento.txt")

_prompt_combinado = _agente
if _conocimiento:
    _prompt_combinado += f"\n\n## Información de productos y precios:\n{_conocimiento}"

SYSTEM_PROMPT = os.environ.get("BOT_SYSTEM_PROMPT", _prompt_combinado)

# ─── ODOO CRM ─────────────────────────────────────────────────────────────────
ODOO_URL     = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
ODOO_DB      = os.environ.get("ODOO_DB", "")

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = os.environ.get("DB_PATH", "/app/bridgebot.db")
