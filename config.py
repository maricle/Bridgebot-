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

SYSTEM_PROMPT = os.environ.get(
    "BOT_SYSTEM_PROMPT",
    "Sos el asistente virtual de Clever CNC, empresa especializada en corte CNC, "
    "laqueado, ranurado y mecanizado de materiales. "
    "Respondé consultas de clientes de forma amable y profesional en español argentino usando 'vos'. "
    "Ayudá con precios, materiales, medidas, plazos y presupuestos. "
    "Si el cliente quiere presupuesto, pedile: tipo de trabajo, material, medidas y cantidad. "
    "Si no podés resolver algo, avisá que un asesor lo va a contactar. "
    "Respondé siempre de forma concisa, máximo 3 oraciones."
)

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "/app/bridgebot.db")
