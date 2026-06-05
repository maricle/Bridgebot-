import logging
import os
import sqlite3

import httpx

from config import DB_PATH, TURSO_TOKEN, TURSO_URL

log = logging.getLogger(__name__)

USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

_CREATE_TABLES = [
    """CREATE TABLE IF NOT EXISTS clientes_odoo (
        odoo_id     INTEGER PRIMARY KEY,
        nombre      TEXT,
        telefono    TEXT,
        email       TEXT,
        synced_at   TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS archivos (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ig_user_id  TEXT NOT NULL,
        canal       TEXT DEFAULT 'whatsapp',
        tipo        TEXT NOT NULL,
        media_id    TEXT DEFAULT '',
        url         TEXT DEFAULT '',
        creado_en   TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS usuarios (
        ig_user_id  TEXT PRIMARY KEY,
        saludado    INTEGER DEFAULT 0,
        canal       TEXT DEFAULT 'instagram',
        creado_en   TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS historial (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ig_user_id  TEXT NOT NULL,
        rol         TEXT NOT NULL,
        contenido   TEXT NOT NULL,
        creado_en   TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS leads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ig_user_id    TEXT NOT NULL,
        canal         TEXT DEFAULT 'instagram',
        resumen       TEXT,
        odoo_lead_id  INTEGER DEFAULT 0,
        creado_en     TEXT DEFAULT (datetime('now'))
    )""",
]


# ─── TURSO HTTP API ────────────────────────────────────────────────────────────

def _arg(val):
    if val is None:
        return {"type": "null"}
    if isinstance(val, int):
        return {"type": "integer", "value": str(val)}
    return {"type": "text", "value": str(val)}


async def _turso(sql: str, args=()) -> dict:
    stmt = {"sql": sql}
    if args:
        stmt["args"] = [_arg(a) for a in args]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TURSO_URL}/v2/pipeline",
            headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
            json={"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]},
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Turso error %s — SQL: %s — Resp: %s", resp.status_code, sql[:80], resp.text[:200])
            resp.raise_for_status()
    result = resp.json()["results"][0]
    if result.get("type") == "error":
        log.error("Turso query error — SQL: %s — Error: %s", sql[:80], result.get("error"))
        raise RuntimeError(result["error"]["message"])
    return result["response"]["result"]


def _rows(result: dict) -> list[dict]:
    cols = [c["name"] for c in result["cols"]]
    return [
        {col: (v["value"] if v["type"] != "null" else None) for col, v in zip(cols, row)}
        for row in result["rows"]
    ]


def _last_id(result: dict) -> int:
    return int(result.get("last_insert_rowid") or 0)


# ─── SQLITE FALLBACK (desarrollo local) ───────────────────────────────────────

def _sqlite_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    for sql in _CREATE_TABLES:
        con.execute(sql)
    con.commit()
    con.close()


def _sqlite_query(sql: str, args=()) -> list[dict]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _sqlite_run(sql: str, args=()) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid or 0


# ─── INTERFAZ UNIFICADA ────────────────────────────────────────────────────────

async def _query(sql: str, args=()) -> list[dict]:
    if USE_TURSO:
        return _rows(await _turso(sql, args))
    return _sqlite_query(sql, args)


async def _run(sql: str, args=()) -> int:
    if USE_TURSO:
        return _last_id(await _turso(sql, args))
    return _sqlite_run(sql, args)


async def _batch_run(statements: list[tuple], chunk: int = 200):
    """Ejecuta múltiples writes en pipelines de hasta `chunk` statements."""
    if not statements:
        return
    if USE_TURSO:
        for i in range(0, len(statements), chunk):
            bloque = statements[i:i + chunk]
            requests = []
            for sql, args in bloque:
                stmt = {"sql": sql}
                if args:
                    stmt["args"] = [_arg(a) for a in args]
                requests.append({"type": "execute", "stmt": stmt})
            requests.append({"type": "close"})
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{TURSO_URL}/v2/pipeline",
                    headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
                    json={"requests": requests},
                    timeout=30,
                )
                resp.raise_for_status()
    else:
        with sqlite3.connect(DB_PATH) as con:
            for sql, args in statements:
                con.execute(sql, args)
            con.commit()


# ─── INIT ──────────────────────────────────────────────────────────────────────

async def init_db():
    if USE_TURSO:
        requests = [{"type": "execute", "stmt": {"sql": sql}} for sql in _CREATE_TABLES]
        requests.append({"type": "close"})
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{TURSO_URL}/v2/pipeline",
                    headers={"Authorization": f"Bearer {TURSO_TOKEN}"},
                    json={"requests": requests},
                    timeout=15,
                )
                resp.raise_for_status()
            log.info("Turso DB lista: %s", TURSO_URL)
        except Exception as e:
            log.critical("ERROR conectando a Turso: %s — la app puede fallar", e)
        for col_sql in [
            "ALTER TABLE usuarios ADD COLUMN cerrada      INTEGER DEFAULT 0",
            "ALTER TABLE usuarios ADD COLUMN nombre       TEXT    DEFAULT ''",
            "ALTER TABLE usuarios ADD COLUMN telefono     TEXT    DEFAULT ''",
            "ALTER TABLE usuarios ADD COLUMN canonical_id TEXT    DEFAULT ''",
        ]:
            try:
                await _turso(col_sql)
            except Exception:
                pass  # columna ya existe
    else:
        log.warning("TURSO_URL/TURSO_TOKEN no configuradas — usando SQLite local (los datos se pierden en cada redeploy)")
        _sqlite_init()
        log.info("SQLite lista (local): %s", DB_PATH)
        for col_sql in [
            "ALTER TABLE usuarios ADD COLUMN cerrada      INTEGER DEFAULT 0",
            "ALTER TABLE usuarios ADD COLUMN nombre       TEXT    DEFAULT ''",
            "ALTER TABLE usuarios ADD COLUMN telefono     TEXT    DEFAULT ''",
            "ALTER TABLE usuarios ADD COLUMN canonical_id TEXT    DEFAULT ''",
        ]:
            try:
                with sqlite3.connect(DB_PATH) as con:
                    con.execute(col_sql)
                    con.commit()
            except Exception:
                pass  # columna ya existe


# ─── FUNCIONES DE NEGOCIO ─────────────────────────────────────────────────────

async def es_usuario_nuevo(user_id: str) -> bool:
    rows = await _query(
        "SELECT saludado FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    return not rows or rows[0]["saludado"] == 0


async def marcar_saludado(user_id: str, canal: str = "instagram"):
    await _run(
        """INSERT INTO usuarios (ig_user_id, saludado, canal) VALUES (?, 1, ?)
           ON CONFLICT(ig_user_id) DO UPDATE SET saludado = 1""",
        (user_id, canal),
    )


async def obtener_historial(user_id: str, limite: int = 10) -> list:
    rows = await _query(
        "SELECT rol, contenido FROM historial WHERE ig_user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limite),
    )
    return [{"role": r["rol"], "content": r["contenido"]} for r in reversed(rows)]


async def guardar_mensaje(user_id: str, rol: str, contenido: str):
    await _run(
        "INSERT INTO historial (ig_user_id, rol, contenido) VALUES (?, ?, ?)",
        (user_id, rol, contenido),
    )


async def guardar_lead(user_id: str, resumen: str, canal: str = "instagram",
                       odoo_lead_id: int = 0) -> int:
    lead_id = await _run(
        "INSERT INTO leads (ig_user_id, canal, resumen, odoo_lead_id) VALUES (?, ?, ?, ?)",
        (user_id, canal, resumen, odoo_lead_id),
    )
    log.info("Lead guardado — user=%s canal=%s odoo_id=%s", user_id, canal, odoo_lead_id)
    return lead_id


async def tiene_lead_activo(user_id: str) -> bool:
    """True si hay un lead creado en las últimas 2 horas (misma sesión)."""
    rows = await _query(
        """SELECT id FROM leads WHERE ig_user_id = ? AND odoo_lead_id > 0
           AND creado_en > datetime('now', '-2 hours')""",
        (user_id,)
    )
    return bool(rows)


async def cerrar_conversacion(user_id: str):
    await _run("UPDATE usuarios SET cerrada = 1 WHERE ig_user_id = ?", (user_id,))


async def resetear_cerrada(user_id: str):
    await _run("UPDATE usuarios SET cerrada = 0 WHERE ig_user_id = ?", (user_id,))


async def obtener_canonical_id(user_id: str) -> str:
    """Devuelve el canonical_id si el usuario está vinculado, sino el mismo user_id."""
    rows = await _query(
        "SELECT canonical_id FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    if rows and rows[0].get("canonical_id"):
        return rows[0]["canonical_id"]
    return user_id


async def buscar_usuario_por_telefono(telefono: str) -> str | None:
    """Busca un usuario de WhatsApp con ese número — su ig_user_id ES el teléfono."""
    rows = await _query(
        "SELECT ig_user_id FROM usuarios WHERE canal = 'whatsapp' AND (ig_user_id = ? OR telefono = ?)",
        (telefono, telefono),
    )
    return rows[0]["ig_user_id"] if rows else None


async def vincular_usuario(user_id: str, canonical_id: str):
    await _run(
        "UPDATE usuarios SET canonical_id = ? WHERE ig_user_id = ?", (canonical_id, user_id)
    )
    log.info("Usuario %s vinculado a canonical %s", user_id, canonical_id)


async def guardar_datos_cliente(user_id: str, nombre: str = "", telefono: str = ""):
    if nombre and telefono:
        await _run(
            "UPDATE usuarios SET nombre = ?, telefono = ? WHERE ig_user_id = ?",
            (nombre, telefono, user_id),
        )
    elif nombre:
        await _run("UPDATE usuarios SET nombre = ? WHERE ig_user_id = ?", (nombre, user_id))
    elif telefono:
        await _run("UPDATE usuarios SET telefono = ? WHERE ig_user_id = ?", (telefono, user_id))


async def obtener_datos_cliente(user_id: str) -> dict:
    rows = await _query(
        "SELECT nombre, telefono FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    if rows:
        return {"nombre": rows[0].get("nombre") or "", "telefono": rows[0].get("telefono") or ""}
    return {"nombre": "", "telefono": ""}


async def conversacion_cerrada(user_id: str) -> bool:
    canonical = await obtener_canonical_id(user_id)
    rows = await _query(
        "SELECT cerrada FROM usuarios WHERE ig_user_id = ? AND cerrada = 1", (canonical,)
    )
    return bool(rows)


async def stats() -> dict:
    u = await _query("SELECT COUNT(*) as n FROM usuarios")
    l = await _query("SELECT COUNT(*) as n FROM leads")
    return {"total_usuarios": u[0]["n"], "total_leads": l[0]["n"]}


async def obtener_leads(limite: int = 50) -> list:
    return await _query(
        "SELECT ig_user_id, canal, resumen, creado_en FROM leads ORDER BY id DESC LIMIT ?",
        (limite,),
    )


async def obtener_usuarios() -> list:
    return await _query(
        "SELECT ig_user_id, canal, saludado, creado_en FROM usuarios ORDER BY creado_en DESC"
    )


async def obtener_conversacion(user_id: str) -> list:
    return await _query(
        "SELECT rol, contenido, creado_en FROM historial WHERE ig_user_id = ? ORDER BY id ASC",
        (user_id,),
    )


async def guardar_archivo(user_id: str, canal: str, tipo: str,
                          media_id: str = "", url: str = ""):
    await _run(
        "INSERT INTO archivos (ig_user_id, canal, tipo, media_id, url) VALUES (?, ?, ?, ?, ?)",
        (user_id, canal, tipo, media_id, url),
    )


async def obtener_archivos(user_id: str) -> list[dict]:
    return await _query(
        "SELECT tipo, media_id, url, creado_en FROM archivos WHERE ig_user_id = ? ORDER BY id ASC",
        (user_id,),
    )


async def resetear_usuario(user_id: str):
    await _run("DELETE FROM usuarios WHERE ig_user_id = ?", (user_id,))
    await _run("DELETE FROM historial WHERE ig_user_id = ?", (user_id,))
    await _run("DELETE FROM leads    WHERE ig_user_id = ?", (user_id,))
    await _run("DELETE FROM archivos WHERE ig_user_id = ?", (user_id,))


# ─── CLIENTES ODOO (sync nocturno) ────────────────────────────────────────────

async def upsert_clientes_odoo(clientes: list[dict]):
    """Bulk upsert de partners de Odoo. clientes: [{odoo_id, nombre, telefono, email}]"""
    if not clientes:
        return
    statements = [
        (
            """INSERT INTO clientes_odoo (odoo_id, nombre, telefono, email, synced_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(odoo_id) DO UPDATE SET
                   nombre=excluded.nombre,
                   telefono=excluded.telefono,
                   email=excluded.email,
                   synced_at=excluded.synced_at""",
            (c["odoo_id"], c["nombre"], c["telefono"], c.get("email", "")),
        )
        for c in clientes
    ]
    await _batch_run(statements)
    log.info("Sync Odoo: %d clientes actualizados en DB local", len(clientes))


async def buscar_cliente_odoo_por_telefono(telefono: str) -> dict | None:
    """Busca por los últimos 10 dígitos (ignora prefijos de país y formato)."""
    digitos = "".join(c for c in telefono if c.isdigit())
    sufijo  = digitos[-10:] if len(digitos) >= 10 else digitos
    if not sufijo:
        return None
    rows = await _query(
        "SELECT odoo_id, nombre, email FROM clientes_odoo WHERE telefono LIKE ?",
        (f"%{sufijo}",),
    )
    return rows[0] if rows else None
