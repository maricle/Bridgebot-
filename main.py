"""
BridgeBot — Instagram → Groq AI Agent
Kleba Dev — 2026
"""

import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
from contextlib import contextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.environ["META_VERIFY_TOKEN"]
APP_SECRET      = os.environ["META_APP_SECRET"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]
IG_ACCOUNT_ID   = os.environ.get("IG_ACCOUNT_ID", "17841456843060136")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
AUTO_RESPUESTA  = os.environ.get("AUTO_RESPUESTA", "false").lower() == "true"

SALUDO = os.environ.get(
    "SALUDO_BIENVENIDA",
    "¡Hola! Soy el asistente virtual de Clever CNC 👋 ¿En qué te puedo ayudar hoy?"
)

SYSTEM_PROMPT = os.environ.get("BOT_SYSTEM_PROMPT",
    "Sos el asistente virtual de Clever CNC, empresa especializada en corte CNC, "
    "laqueado, ranurado y mecanizado de materiales. "
    "Respondé consultas de clientes de forma amable y profesional en español argentino usando 'vos'. "
    "Ayudá con precios, materiales, medidas, plazos y presupuestos. "
    "Si el cliente quiere presupuesto, pedile: tipo de trabajo, material, medidas y cantidad. "
    "Si no podés resolver algo, avisá que un asesor lo va a contactar. "
    "Respondé siempre de forma concisa, máximo 3 oraciones."
)

DB_PATH = os.environ.get("DB_PATH", "/app/bridgebot.db")

# ─── APP ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="BridgeBot", version="4.0.0")


# ─── BASE DE DATOS SQLite ─────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            ig_user_id  TEXT PRIMARY KEY,
            saludado    INTEGER DEFAULT 0,
            creado_en   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS historial (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ig_user_id  TEXT NOT NULL,
            rol         TEXT NOT NULL,
            contenido   TEXT NOT NULL,
            creado_en   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS leads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ig_user_id  TEXT NOT NULL,
            resumen     TEXT,
            creado_en   TEXT DEFAULT (datetime('now'))
        );
    """)
    con.commit()
    con.close()
    log.info("Base de datos lista: %s", DB_PATH)


@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def es_usuario_nuevo(ig_user_id: str) -> bool:
    with get_db() as con:
        row = con.execute(
            "SELECT saludado FROM usuarios WHERE ig_user_id = ?", (ig_user_id,)
        ).fetchone()
        return row is None or row["saludado"] == 0


def marcar_saludado(ig_user_id: str):
    with get_db() as con:
        con.execute("""
            INSERT INTO usuarios (ig_user_id, saludado)
            VALUES (?, 1)
            ON CONFLICT(ig_user_id) DO UPDATE SET saludado = 1
        """, (ig_user_id,))
        con.commit()


def obtener_historial(ig_user_id: str, limite: int = 20) -> list:
    with get_db() as con:
        rows = con.execute("""
            SELECT rol, contenido FROM historial
            WHERE ig_user_id = ?
            ORDER BY id DESC LIMIT ?
        """, (ig_user_id, limite)).fetchall()
    return [{"role": r["rol"], "content": r["contenido"]} for r in reversed(rows)]


def guardar_mensaje(ig_user_id: str, rol: str, contenido: str):
    with get_db() as con:
        con.execute(
            "INSERT INTO historial (ig_user_id, rol, contenido) VALUES (?, ?, ?)",
            (ig_user_id, rol, contenido)
        )
        con.commit()


def guardar_lead(ig_user_id: str, resumen: str):
    with get_db() as con:
        con.execute(
            "INSERT INTO leads (ig_user_id, resumen) VALUES (?, ?)",
            (ig_user_id, resumen)
        )
        con.commit()
    log.info("Lead guardado para IG user=%s", ig_user_id)


# ─── GROQ AI ─────────────────────────────────────────────────────────────────

async def generar_respuesta_groq(ig_user_id: str, mensaje: str) -> str:
    if not GROQ_API_KEY:
        return "El servicio de IA no está configurado. Te contactamos a la brevedad."

    guardar_mensaje(ig_user_id, "user", mensaje)
    msgs = obtener_historial(ig_user_id)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + msgs,
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
                timeout=25,
            )

            if resp.status_code != 200:
                log.error("Groq error %s: %s", resp.status_code, resp.text)
                return "En este momento no puedo responderte. Te contactamos a la brevedad."

            data = resp.json()
            respuesta = data["choices"][0]["message"]["content"].strip()

    except httpx.TimeoutException:
        log.error("Groq timeout para user=%s", ig_user_id)
        return "Tardamos un poco más de lo normal. ¿Podés repetir tu consulta?"
    except Exception as e:
        log.error("Groq excepción: %s", e)
        return "Tuvimos un problema técnico. Te contactamos a la brevedad."

    guardar_mensaje(ig_user_id, "assistant", respuesta)

    palabras_lead = ["presupuesto", "precio", "cuánto", "cuanto", "cotización",
                     "medidas", "cantidad", "encargar", "necesito", "quiero"]
    if any(p in mensaje.lower() for p in palabras_lead):
        guardar_lead(ig_user_id, f"Mensaje: {mensaje[:200]}")

    log.info("Groq → IG user=%s: %s...", ig_user_id, respuesta[:80])
    return respuesta


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────

async def enviar_mensaje_instagram(client: httpx.AsyncClient, recipient_id: str, texto: str) -> bool:
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": texto},
        "messaging_type": "RESPONSE",
    }
    headers = {
        "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    endpoints = [
        f"https://graph.instagram.com/v19.0/{IG_ACCOUNT_ID}/messages",
        f"https://graph.facebook.com/v19.0/{IG_ACCOUNT_ID}/messages",
    ]
    for url in endpoints:
        try:
            resp = await client.post(url, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                log.info("DM enviado a IG user=%s", recipient_id)
                return True
            log.warning("%s → %s: %s", url.split("/")[2], resp.status_code, resp.text)
        except Exception as e:
            log.warning("Excepción DM %s: %s", url.split("/")[2], e)
    log.error("No se pudo enviar DM a IG user=%s", recipient_id)
    return False


# ─── SEGURIDAD ────────────────────────────────────────────────────────────────

def verificar_firma(payload: bytes, firma_header: str) -> bool:
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(APP_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(firma_header[7:], firma_esperada)


def extraer_sender_y_mensaje(data: dict) -> tuple[str, str]:
    entry = data.get("entry", [{}])[0]
    for msg in entry.get("messaging", []):
        sender_id = msg.get("sender", {}).get("id", "")
        texto     = msg.get("message", {}).get("text", "")
        if sender_id and texto:
            return sender_id, texto
    for change in entry.get("changes", []):
        value = change.get("value", {})
        for msg in value.get("messages", []):
            sender_id = msg.get("from", {}).get("id") or value.get("contacts", [{}])[0].get("wa_id", "")
            texto     = msg.get("text", {}).get("body", "")
            if sender_id and texto:
                return sender_id, texto
    return "", ""


# ─── RUTAS ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    log.info("BridgeBot v4 iniciado — modo: %s", "AUTO_RESPUESTA" if AUTO_RESPUESTA else "GROQ_AI")
    log.info("GROQ_API_KEY configurada: %s", "SI" if GROQ_API_KEY else "NO")
    log.info("IG_ACCOUNT_ID: %s", IG_ACCOUNT_ID or "NO CONFIGURADO")


@app.get("/webhook")
async def verificar_webhook(request: Request):
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token incorrecto")


@app.post("/webhook")
async def recibir_mensaje(request: Request):
    payload = await request.body()
    firma   = request.headers.get("X-Hub-Signature-256", "")
    if not verificar_firma(payload, firma):
        raise HTTPException(status_code=401, detail="Firma inválida")
    data = await request.json()
    entry = data.get("entry", [{}])[0]
    log.info("Webhook [%s] entry=%s", data.get("object", "?"), entry.get("id", "?"))
    asyncio.create_task(procesar_evento(data))
    return Response(status_code=200)


async def procesar_evento(data: dict):
    try:
        sender_id, mensaje = extraer_sender_y_mensaje(data)

        if not sender_id or not mensaje:
            log.info("Evento sin texto, ignorando.")
            return

        if sender_id == IG_ACCOUNT_ID:
            log.info("Mensaje propio del bot, ignorando.")
            return

        log.info("IG user=%s: %s", sender_id, mensaje[:100])

        async with httpx.AsyncClient() as client:

            if AUTO_RESPUESTA:
                if es_usuario_nuevo(sender_id):
                    await enviar_mensaje_instagram(client, sender_id, SALUDO)
                    marcar_saludado(sender_id)
                    log.info("Saludo enviado a nuevo usuario %s", sender_id)
                else:
                    log.info("Usuario %s ya saludado, ignorando en modo AUTO.", sender_id)
                return

            # ── Modo Groq AI ──────────────────────────────────────────────────
            nuevo = es_usuario_nuevo(sender_id)
            if nuevo:
                marcar_saludado(sender_id)

            respuesta = await generar_respuesta_groq(sender_id, mensaje)

            if nuevo:
                respuesta = f"{SALUDO}\n\n{respuesta}"

            await enviar_mensaje_instagram(client, sender_id, respuesta)

    except Exception as e:
        log.exception("Error procesando evento: %s", e)


@app.get("/health")
async def health():
    with get_db() as con:
        total_usuarios = con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        total_leads    = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    return {
        "status": "ok",
        "version": "4.0.0",
        "modo": "AUTO_RESPUESTA" if AUTO_RESPUESTA else "GROQ_AI",
        "groq_configurado": bool(GROQ_API_KEY),
        "total_usuarios": total_usuarios,
        "total_leads": total_leads,
    }


@app.get("/leads")
async def ver_leads():
    with get_db() as con:
        rows = con.execute(
            "SELECT ig_user_id, resumen, creado_en FROM leads ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]
