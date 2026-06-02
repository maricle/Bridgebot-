import logging
import os

import libsql_client

from config import DB_PATH, TURSO_TOKEN, TURSO_URL

log = logging.getLogger(__name__)

# Si hay TURSO_URL usa la nube, sino usa SQLite local
_db_url = TURSO_URL if TURSO_URL else f"file:{DB_PATH}"
_db_token = TURSO_TOKEN if TURSO_TOKEN else None


def _get_client():
    return libsql_client.create_client_async(url=_db_url, auth_token=_db_token)


CREATE_TABLES = """
    CREATE TABLE IF NOT EXISTS usuarios (
        ig_user_id  TEXT PRIMARY KEY,
        saludado    INTEGER DEFAULT 0,
        canal       TEXT DEFAULT 'instagram',
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
        canal       TEXT DEFAULT 'instagram',
        resumen     TEXT,
        creado_en   TEXT DEFAULT (datetime('now'))
    );
"""


async def init_db():
    if not TURSO_URL:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with _get_client() as db:
        for stmt in [s.strip() for s in CREATE_TABLES.split(";") if s.strip()]:
            await db.execute(stmt)
    log.info("Base de datos lista: %s", _db_url)


async def es_usuario_nuevo(user_id: str) -> bool:
    async with _get_client() as db:
        result = await db.execute(
            "SELECT saludado FROM usuarios WHERE ig_user_id = ?", [user_id]
        )
        if not result.rows:
            return True
        return result.rows[0][0] == 0


async def marcar_saludado(user_id: str, canal: str = "instagram"):
    async with _get_client() as db:
        await db.execute(
            """INSERT INTO usuarios (ig_user_id, saludado, canal) VALUES (?, 1, ?)
               ON CONFLICT(ig_user_id) DO UPDATE SET saludado = 1""",
            [user_id, canal]
        )


async def obtener_historial(user_id: str, limite: int = 20) -> list:
    async with _get_client() as db:
        result = await db.execute(
            "SELECT rol, contenido FROM historial WHERE ig_user_id = ? ORDER BY id DESC LIMIT ?",
            [user_id, limite]
        )
    rows = list(reversed(result.rows))
    return [{"role": r[0], "content": r[1]} for r in rows]


async def guardar_mensaje(user_id: str, rol: str, contenido: str):
    async with _get_client() as db:
        await db.execute(
            "INSERT INTO historial (ig_user_id, rol, contenido) VALUES (?, ?, ?)",
            [user_id, rol, contenido]
        )


async def guardar_lead(user_id: str, resumen: str, canal: str = "instagram"):
    async with _get_client() as db:
        await db.execute(
            "INSERT INTO leads (ig_user_id, canal, resumen) VALUES (?, ?, ?)",
            [user_id, canal, resumen]
        )
    log.info("Lead guardado — user=%s canal=%s", user_id, canal)


async def stats() -> dict:
    async with _get_client() as db:
        r1 = await db.execute("SELECT COUNT(*) FROM usuarios")
        r2 = await db.execute("SELECT COUNT(*) FROM leads")
    return {
        "total_usuarios": r1.rows[0][0],
        "total_leads": r2.rows[0][0],
    }


async def obtener_leads(limite: int = 50) -> list:
    async with _get_client() as db:
        result = await db.execute(
            "SELECT ig_user_id, canal, resumen, creado_en FROM leads ORDER BY id DESC LIMIT ?",
            [limite]
        )
    cols = ["ig_user_id", "canal", "resumen", "creado_en"]
    return [dict(zip(cols, r)) for r in result.rows]
