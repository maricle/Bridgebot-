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

_BASE_PROMPT = (
    "Sos el asistente virtual de {empresa}. "
    "Respondé consultas de clientes de forma amable y profesional en español argentino usando 'vos'. "
    "Usá la información de la empresa para responder con precisión. "
    "Si el cliente quiere presupuesto, pedile: tipo de trabajo, material, medidas y cantidad. "
    "Si no podés resolver algo, avisá que un asesor lo va a contactar. "
    "IMPORTANTE: nunca saludes con 'Hola' ni te presentes — el saludo ya fue enviado. "
    "Respondé siempre de forma concisa, máximo 3 oraciones."
)

def _cargar_conocimiento() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ruta = os.path.join(base_dir, "conocimiento.txt")
    try:
        with open(ruta, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""

_conocimiento = _cargar_conocimiento()
_empresa = os.environ.get("EMPRESA_NOMBRE", "la empresa")
_prompt_base = _BASE_PROMPT.format(empresa=_empresa)

SYSTEM_PROMPT = os.environ.get(
    "BOT_SYSTEM_PROMPT",
    f"{_prompt_base}\n\n## Información de la empresa:\n{_conocimiento}" if _conocimiento else _prompt_base
)

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = os.environ.get("DB_PATH", "/app/bridgebot.db")
