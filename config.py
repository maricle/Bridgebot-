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

# ─── MODO DEV ─────────────────────────────────────────────────────────────────
# Bloquea efectos externos reales (envío de WhatsApp y creación de leads en Odoo)
# para poder probar el flujo de conversación sin impactar producción.
MODO_DEV = os.environ.get("MODO_DEV", "false").lower() == "true"

SALUDO = os.environ.get("SALUDO_BIENVENIDA", "¡Hola! 👋 ¿En qué te puedo ayudar hoy?")
BOT_NOMBRE = os.environ.get("BOT_NOMBRE", "Asistente")

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
NOMBRE_NEGOCIO   = os.environ.get("NOMBRE_NEGOCIO", "BridgeBot")
DASHBOARD_COLOR  = os.environ.get("DASHBOARD_COLOR", "#4f46e5")

# ─── PANEL DE CONFIGURACIÓN (overrides guardados en DB) ───────────────────────
# Variables editables desde /dashboard sin pasar por Railway. El valor de arriba
# (env var) es el default; si hay un override guardado desde el panel, gana ese.
KNOWLEDGE_ARCHIVOS = ["agente.md", "conocimiento.md", "01_reglas_comerciales.md", "areas.json", "precios.md"]


async def recargar_configuracion():
    """Vuelve a leer los overrides de configuracion (tabla `configuracion`, claves
    `config:*`) y actualiza las variables en memoria. Se llama al arrancar y cada
    vez que se guarda algo desde el panel — nunca desde el hot path por mensaje."""
    global SALUDO, ALIAS_TRANSFERENCIA, AUTO_RESPUESTA, NOMBRE_NEGOCIO, DASHBOARD_COLOR
    from db import obtener_config_todas
    overrides = await obtener_config_todas()

    if "config:SALUDO_BIENVENIDA" in overrides:
        SALUDO = overrides["config:SALUDO_BIENVENIDA"]
    if "config:ALIAS_TRANSFERENCIA" in overrides:
        ALIAS_TRANSFERENCIA = overrides["config:ALIAS_TRANSFERENCIA"]
    if "config:AUTO_RESPUESTA" in overrides:
        AUTO_RESPUESTA = overrides["config:AUTO_RESPUESTA"].lower() == "true"
    if "config:NOMBRE_NEGOCIO" in overrides:
        NOMBRE_NEGOCIO = overrides["config:NOMBRE_NEGOCIO"]
    if "config:DASHBOARD_COLOR" in overrides:
        DASHBOARD_COLOR = overrides["config:DASHBOARD_COLOR"]


async def recargar_conocimiento():
    """Vuelve a armar agente.md/conocimiento.md/areas.json a partir de overrides
    guardados desde el panel (tabla `configuracion`, claves `knowledge:*`), con
    fallback a los archivos en disco de siempre. No incluye precios.md — ese lo
    maneja precios.py directamente."""
    global _agente, _conocimiento, _AREAS_CONFIG, _ARCHIVOS, _AREAS, _FLUJO_CACHE
    from db import obtener_config

    agente_ov = await obtener_config("knowledge:agente.md")
    _agente = agente_ov if agente_ov is not None else _leer_archivo("agente.md")

    conocimiento_ov = await obtener_config("knowledge:conocimiento.md")
    conocimiento_base = conocimiento_ov if conocimiento_ov is not None else _leer_archivo("conocimiento.md")
    reglas_ov = await obtener_config("knowledge:01_reglas_comerciales.md")
    reglas = reglas_ov if reglas_ov is not None else _leer_archivo("01_reglas_comerciales.md")
    _conocimiento = "\n\n---\n\n".join(p for p in [conocimiento_base, reglas] if p)

    areas_ov = await obtener_config("knowledge:areas.json")
    if areas_ov is not None:
        import json
        try:
            _AREAS_CONFIG = json.loads(areas_ov)
        except json.JSONDecodeError:
            pass  # deja la version anterior si el JSON guardado quedo invalido
    else:
        _AREAS_CONFIG = _leer_areas()
    _ARCHIVOS = {a["id"]: a for a in _AREAS_CONFIG.get("archivos", [])}
    _AREAS    = {a["id"]: a for a in _AREAS_CONFIG.get("areas", [])}
    _FLUJO_CACHE = {}


async def obtener_knowledge_efectivo() -> dict[str, str]:
    """{archivo: contenido} actual de cada archivo gestionable desde el panel
    (override de DB si existe, sino el archivo en disco)."""
    from db import obtener_config
    from precios import obtener as obtener_precios
    resultado = {}
    for nombre in KNOWLEDGE_ARCHIVOS:
        if nombre == "precios.md":
            resultado[nombre] = obtener_precios()
            continue
        override = await obtener_config(f"knowledge:{nombre}")
        resultado[nombre] = override if override is not None else _leer_archivo(nombre)
    return resultado

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


def _leer_areas() -> dict:
    """Carga knowledge/areas.json — manifest de áreas/rubros del negocio.
    Opcional: si no existe (ej. negocios sin flujos por área), queda vacío."""
    import json
    base_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base_dir, "knowledge", "areas.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_AREAS_CONFIG  = _leer_areas()
_ARCHIVOS      = {a["id"]: a for a in _AREAS_CONFIG.get("archivos", [])}
_AREAS         = {a["id"]: a for a in _AREAS_CONFIG.get("areas", [])}
_FLUJO_CACHE: dict[str, str] = {}


def _contenido_flujo(archivo_id: str) -> str:
    if archivo_id not in _FLUJO_CACHE:
        meta = _ARCHIVOS.get(archivo_id, {})
        _FLUJO_CACHE[archivo_id] = _leer_archivo(meta["archivo"]) if meta.get("archivo") else ""
    return _FLUJO_CACHE[archivo_id]


def _tabla_areas_markdown() -> str:
    areas = _AREAS_CONFIG.get("areas", [])
    if not areas:
        return ""
    filas = "\n".join(f"| {', '.join(a['keywords'])} | **{a['nombre']}** |" for a in areas)
    return (
        "Si el cliente menciona alguna de estas palabras o conceptos, identificá el área "
        "correspondiente antes de seguir preguntando:\n\n"
        f"| Si menciona... | Área |\n|---|---|\n{filas}"
    )


PALABRAS_PRECIO = {
    "precio", "precios", "presupuesto", "costo", "costos",
    "cuanto", "cuánto", "vale", "sale", "tarifa", "valor",
    "cotizacion", "cotización", "plata", "pesos", "cobran", "cobras",
}


def areas_categorias() -> dict[str, list[str]]:
    """Devuelve {nombre_area: keywords} de todas las áreas de knowledge/areas.json.
    Útil para agrupar consultas por rubro (ej. analytics) sin listas hardcodeadas."""
    return {a["nombre"]: a.get("keywords", []) for a in _AREAS.values()}


def detectar_areas(mensaje: str) -> list[str]:
    """Devuelve los ids de área cuyas keywords aparecen en el mensaje."""
    texto = mensaje.lower()
    return [
        area_id for area_id, area in _AREAS.items()
        if any(kw in texto for kw in area.get("keywords", []))
    ]


def area_bloquea_precios(areas_detectadas: list[str]) -> bool:
    """True si alguna de las áreas detectadas está marcada sin_precio (trabajos a medida)."""
    return any(_AREAS.get(area_id, {}).get("sin_precio") for area_id in areas_detectadas)


def resolver_destino_odoo(areas_detectadas: list[str]) -> str:
    """Devuelve el destino Odoo (clave de ODOO_DESTINOS) de la primera área detectada
    que tenga 'odoo_destino' configurado en areas.json. Si ninguna aplica, "default"."""
    for area_id in areas_detectadas:
        destino = _AREAS.get(area_id, {}).get("odoo_destino")
        if destino:
            return destino
    return "default"


_PROMPT_BASE = os.environ.get("BOT_SYSTEM_PROMPT", "")


def get_system_prompt(con_precios: bool = False, canal: str = "instagram",
                      areas_detectadas: list[str] | None = None) -> str:
    from precios import obtener as obtener_precios
    base = _PROMPT_BASE or _agente
    base += f"\n\n## Canal actual: {canal.upper()}"
    if _conocimiento:
        base += f"\n\n## Información de la empresa:\n{_conocimiento}"
    if WA_NUMERO_SOPORTE:
        base += f"\n\n## Seguimiento de pedidos en curso:\nEnlace directo al equipo de producción: https://wa.me/{WA_NUMERO_SOPORTE}"

    tabla_areas = _tabla_areas_markdown()
    if tabla_areas:
        base += f"\n\n## Áreas y flujos del negocio\n{tabla_areas}"

    archivos_incluidos: set[str] = set()
    for area_id in areas_detectadas or []:
        area = _AREAS.get(area_id)
        if not area:
            continue
        if area.get("instrucciones"):
            base += f"\n\n## {area['nombre']} — instrucciones:\n{area['instrucciones']}"
        archivo_id = area.get("archivo_id")
        if archivo_id and archivo_id not in archivos_incluidos:
            archivos_incluidos.add(archivo_id)
            contenido = _contenido_flujo(archivo_id)
            if contenido:
                nombre = _ARCHIVOS.get(archivo_id, {}).get("nombre", archivo_id)
                base += f"\n\n## Flujo de atención — {nombre}:\n{contenido}"

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

# Routing multi-company genérico: cualquier negocio con más de una empresa/responsable
# en Odoo puede definir env vars ODOO_DESTINO_<CLAVE>="company_id:user_id" (ej.
# ODOO_DESTINO_OFICINA="5:10") y referenciar <clave> desde el campo "odoo_destino" de
# cada área en knowledge/areas.json. Si no hay ninguna definida, se usa la empresa/
# usuario por defecto (ver _resolver_destino en odoo_crm.py).
ODOO_DESTINOS = {
    k[len("ODOO_DESTINO_"):].lower(): v
    for k, v in os.environ.items()
    if k.startswith("ODOO_DESTINO_") and v
}

# Usuarios adicionales a notificar al crear un lead (IDs separados por coma, ej. "3,7")
ODOO_NOTIFICAR_USUARIOS = [
    int(u.strip()) for u in os.environ.get("ODOO_NOTIFICAR_USUARIOS", "").split(",")
    if u.strip().isdigit()
]

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────
TURSO_URL   = os.environ.get("TURSO_URL", "").replace("libsql://", "https://")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")
DB_PATH     = os.environ.get("DB_PATH", "/app/bridgebot.db")

# ─── API EXTERNA ──────────────────────────────────────────────────────────────
# Clave para endpoints que llaman servicios externos (ej: Odoo)
BRIDGE_API_KEY = os.environ.get("BRIDGE_API_KEY", "")

# Alias de transferencia bancaria informado en el mensaje de orden confirmada
ALIAS_TRANSFERENCIA = os.environ.get("ALIAS_TRANSFERENCIA", "")

# Plantillas de mensajes WA para notificaciones desde Odoo
# Placeholders disponibles: {nombre}, {nro_orden}, {monto}, {empresa} (los últimos dos solo en orden confirmada)
# orden confirmada se envía como plantilla de Meta ("presupuesto_2") — este texto es
# solo el que queda registrado en el historial/chatter, debe reflejar el body aprobado.
WA_MSG_ORDEN_CONFIRMADA = os.environ.get(
    "WA_MSG_ORDEN_CONFIRMADA",
    "Hola {nombre},\n\n"
    "Te enviamos el presupuesto solicitado, *{nro_orden}* de *{empresa}*, por un total de *{monto}*.\n\n"
    "Alias :  *Gideas.oficina* - Clelia Fernández (oficina)\n\n"
    "Enviar comprobante de la seña por favor.\n\n"
    "Gracias."
)
WA_MSG_TRABAJO_LISTO = os.environ.get(
    "WA_MSG_TRABAJO_LISTO",
    "Hola {nombre} 👋 Tu pedido *#{nro_orden}* ya está listo. "
    "¡Podés pasar a retirarlo cuando quieras!"
)

# Nombres de las plantillas de Meta para "trabajo listo" según sucursal —
# ajustar por env var si el nombre real aprobado difiere del default.
WA_PLANTILLA_TRABAJO_LISTO_TALLER = os.environ.get("WA_PLANTILLA_TRABAJO_LISTO_TALLER", "trabajo_listo_taller")
WA_PLANTILLA_TRABAJO_LISTO_OFICINA = os.environ.get("WA_PLANTILLA_TRABAJO_LISTO_OFICINA", "trabajo_listo")
