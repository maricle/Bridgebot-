import logging
import os
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

log = logging.getLogger(__name__)


def _init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
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
    """)
    con.commit()
    con.close()


@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


async def init_db():
    _init()
    log.info("Base de datos lista: %s", DB_PATH)


async def es_usuario_nuevo(user_id: str) -> bool:
    with _db() as con:
        row = con.execute(
            "SELECT saludado FROM usuarios WHERE ig_user_id = ?", (user_id,)
        ).fetchone()
        return row is None or row["saludado"] == 0


async def marcar_saludado(user_id: str, canal: str = "instagram"):
    with _db() as con:
        con.execute("""
            INSERT INTO usuarios (ig_user_id, saludado, canal) VALUES (?, 1, ?)
            ON CONFLICT(ig_user_id) DO UPDATE SET saludado = 1
        """, (user_id, canal))


async def obtener_historial(user_id: str, limite: int = 20) -> list:
    with _db() as con:
        rows = con.execute("""
            SELECT rol, contenido FROM historial
            WHERE ig_user_id = ? ORDER BY id DESC LIMIT ?
        """, (user_id, limite)).fetchall()
    return [{"role": r["rol"], "content": r["contenido"]} for r in reversed(rows)]


async def guardar_mensaje(user_id: str, rol: str, contenido: str):
    with _db() as con:
        con.execute(
            "INSERT INTO historial (ig_user_id, rol, contenido) VALUES (?, ?, ?)",
            (user_id, rol, contenido)
        )


async def guardar_lead(user_id: str, resumen: str, canal: str = "instagram"):
    with _db() as con:
        con.execute(
            "INSERT INTO leads (ig_user_id, canal, resumen) VALUES (?, ?, ?)",
            (user_id, canal, resumen)
        )
    log.info("Lead guardado — user=%s canal=%s", user_id, canal)


async def stats() -> dict:
    with _db() as con:
        usuarios = con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        leads    = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    return {"total_usuarios": usuarios, "total_leads": leads}


async def obtener_leads(limite: int = 50) -> list:
    with _db() as con:
        rows = con.execute(
            "SELECT ig_user_id, canal, resumen, creado_en FROM leads ORDER BY id DESC LIMIT ?",
            (limite,)
        ).fetchall()
    return [dict(r) for r in rows]


async def obtener_usuarios() -> list:
    with _db() as con:
        rows = con.execute(
            "SELECT ig_user_id, canal, saludado, creado_en FROM usuarios ORDER BY creado_en DESC"
        ).fetchall()
    return [dict(r) for r in rows]


async def obtener_conversacion(user_id: str) -> list:
    with _db() as con:
        rows = con.execute(
            "SELECT rol, contenido, creado_en FROM historial WHERE ig_user_id = ? ORDER BY id ASC",
            (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


async def resetear_usuario(user_id: str):
    with _db() as con:
        con.execute("DELETE FROM usuarios WHERE ig_user_id = ?", (user_id,))
        con.execute("DELETE FROM historial WHERE ig_user_id = ?", (user_id,))
        con.execute("DELETE FROM leads WHERE ig_user_id = ?", (user_id,))
