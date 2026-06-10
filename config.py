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
WA_ACCESS_TOKEN    = os.environ.get("WA_ACCESS_TOKEN", "")
WA_PHONE_ID        = os.environ.get("WA_PHONE_ID", "")
WA_NUMERO_SOPORTE  = os.environ.get("WA_NUMERO_SOPORTE", "")  # número para derivar seguimiento de pedidos

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


def _leer_conocimiento_base() -> str:
    """Carga conocimiento.md + 01_* (reglas generales, siempre presentes)."""
    import glob
    base_dir = os.path.dirname(os.path.abspath(__file__))
    knowledge_dir = os.path.join(base_dir, "knowledge")
    partes = [_leer_archivo("conocimiento.md")]
    for filepath in sorted(glob.glob(os.path.join(knowledge_dir, "01_*.md"))):
        contenido = _leer_archivo(os.path.basename(filepath))
        if contenido:
            partes.append(contenido)
    return "\n\n---\n\n".join(p for p in partes if p)


_agente              = _leer_archivo("agente.md")
_conocimiento        = _leer_conocimiento_base()
_flujo_carteleria    = _leer_archivo("02_flujo_carteleria.md")
_flujo_grafica       = _leer_archivo("03_flujo_grafica_impresiones.md")

_PROMPT_BASE = os.environ.get("BOT_SYSTEM_PROMPT", "")


def get_system_prompt(con_precios: bool = False, canal: str = "instagram",
                      flujo: str | None = None) -> str:
    from precios import obtener as obtener_precios
    base = _PROMPT_BASE or _agente
    base += f"\n\n## Canal actual: {canal.upper()}"
    if _conocimiento:
        base += f"\n\n## Información de la empresa:\n{_conocimiento}"
    if WA_NUMERO_SOPORTE:
        base += f"\n\n## Seguimiento de pedidos en curso:\nEnlace directo al equipo de producción: https://wa.me/{WA_NUMERO_SOPORTE}"
    if flujo == "carteleria" and _flujo_carteleria:
        base += f"\n\n## Flujo de atención — Cartelería y Gran Formato:\n{_flujo_carteleria}"
    elif flujo == "grafica" and _flujo_grafica:
        base += f"\n\n## Flujo de atención — Gráfica e Impresiones:\n{_flujo_grafica}"
    elif flujo == "ambos":
        if _flujo_carteleria:
            base += f"\n\n## Flujo de atención — Cartelería y Gran Formato:\n{_flujo_carteleria}"
        if _flujo_grafica:
            base += f"\n\n## Flujo de atención — Gráfica e Impresiones:\n{_flujo_grafica}"
    if con_precios:
        precios = obtener_precios()
        if precios:
            base += f"\n\n## Lista de precios:\n{precios}"
    return base

# ─── ODOO CRM ─────────────────────────────────────────────────────────────────
ODOO_URL     = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")
ODOO_DB      = os.environ.get("ODOO_DB", "")
ODOO_LOGIN   = os.environ.get("ODOO_LOGIN", "")

# Routing multi-company (Grupo Ideas)
# Formato: "company_id:user_id" — ej. "3:5" → company_id=3, user_id=5
ODOO_DESTINO_CARTELERIA = os.environ.get("ODOO_DESTINO_CARTELERIA", "")
ODOO_DESTINO_OFICINA    = os.environ.get("ODOO_DESTINO_OFICINA", "")

# Usuarios adicionales a notificar al crear un lead (IDs separados por coma, ej. "3,7")
ODOO_NOTIFICAR_USUARIOS = [
    int(u.strip()) for u in os.environ.get("ODOO_NOTIFICAR_USUARIOS", "").split(",")
    if u.strip().isdigit()
]

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
TURSO_URL   = os.environ.get("TURSO_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = os.environ.get("DB_PATH", "/app/bridgebot.db")
