import asyncio
import datetime
import logging

import asyncpg

from config import DATABASE_URL

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_CREATE_TABLES = [
    """CREATE TABLE IF NOT EXISTS clientes_odoo (
        odoo_id     INTEGER PRIMARY KEY,
        nombre      TEXT,
        telefono    TEXT,
        email       TEXT,
        synced_at   TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS mensajes_procesados (
        message_id   TEXT PRIMARY KEY,
        procesado_en TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS archivos (
        id          SERIAL PRIMARY KEY,
        ig_user_id  TEXT NOT NULL,
        canal       TEXT DEFAULT 'whatsapp',
        tipo        TEXT NOT NULL,
        media_id    TEXT DEFAULT '',
        url         TEXT DEFAULT '',
        creado_en   TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS usuarios (
        ig_user_id  TEXT PRIMARY KEY,
        saludado    INTEGER DEFAULT 0,
        canal       TEXT DEFAULT 'instagram',
        creado_en   TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS historial (
        id          SERIAL PRIMARY KEY,
        ig_user_id  TEXT NOT NULL,
        rol         TEXT NOT NULL,
        contenido   TEXT NOT NULL,
        creado_en   TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS leads (
        id            SERIAL PRIMARY KEY,
        ig_user_id    TEXT NOT NULL,
        canal         TEXT DEFAULT 'instagram',
        resumen       TEXT,
        odoo_lead_id  INTEGER DEFAULT 0,
        creado_en     TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS tareas_odoo (
        odoo_id          INTEGER PRIMARY KEY,
        task_name        TEXT NOT NULL,
        nro_orden        TEXT DEFAULT '',
        stage            TEXT DEFAULT '',
        partner_name     TEXT DEFAULT '',
        telefono         TEXT DEFAULT '',
        documento        TEXT DEFAULT '',
        sale_order_name  TEXT DEFAULT '',
        synced_at        TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS configuracion (
        clave          TEXT PRIMARY KEY,
        valor          TEXT NOT NULL DEFAULT '',
        actualizado_en TIMESTAMPTZ DEFAULT now()
    )""",
]

_ALTER_COLUMNS = [
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS cerrada      INTEGER DEFAULT 0",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre       TEXT    DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS telefono     TEXT    DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS canonical_id TEXT    DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email        TEXT    DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS pausado      INTEGER DEFAULT 0",
    "ALTER TABLE archivos ADD COLUMN IF NOT EXISTS es_comprobante    INTEGER DEFAULT 0",
    "ALTER TABLE archivos ADD COLUMN IF NOT EXISTS datos_comprobante TEXT    DEFAULT ''",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultima_orden_id  INTEGER DEFAULT 0",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultima_orden_nro TEXT    DEFAULT ''",
]


# ─── INTERFAZ UNIFICADA (Postgres via asyncpg) ─────────────────────────────────

def _pg(sql: str) -> str:
    """Traduce placeholders estilo sqlite (`?`) a los de asyncpg (`$1, $2, ...`),
    en orden de aparición — ninguna de las queries de este archivo usa `?` como
    literal, así que un reemplazo secuencial simple es seguro."""
    partes = sql.split("?")
    resultado = partes[0]
    for i, parte in enumerate(partes[1:], start=1):
        resultado += f"${i}" + parte
    return resultado


def _normalizar(valor):
    if isinstance(valor, (datetime.datetime, datetime.date)):
        return valor.isoformat()
    return valor


def _row_a_dict(record: asyncpg.Record) -> dict:
    return {k: _normalizar(v) for k, v in dict(record).items()}


async def _query(sql: str, args=()) -> list[dict]:
    rows = await _pool.fetch(_pg(sql), *args)
    return [_row_a_dict(r) for r in rows]


async def _run(sql: str, args=()) -> int:
    if "RETURNING id" in sql:
        return await _pool.fetchval(_pg(sql), *args) or 0
    await _pool.execute(_pg(sql), *args)
    return 0


async def _batch_run(statements: list[tuple]):
    """Ejecuta múltiples writes dentro de una sola transacción."""
    if not statements:
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            for sql, args in statements:
                await conn.execute(_pg(sql), *args)


# ─── INIT ──────────────────────────────────────────────────────────────────────

async def init_db():
    global _pool
    if not DATABASE_URL:
        log.critical("DATABASE_URL no configurada — la app no puede conectar a Postgres")
        return
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        for sql in _CREATE_TABLES:
            await conn.execute(sql)
        for col_sql in _ALTER_COLUMNS:
            await conn.execute(col_sql)
    log.info("Postgres lista")


async def cerrar_pool():
    if _pool is not None:
        await _pool.close()


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


async def guardar_mensaje(user_id: str, rol: str, contenido: str, notificar_odoo: bool = True):
    await _run(
        "INSERT INTO historial (ig_user_id, rol, contenido) VALUES (?, ?, ?)",
        (user_id, rol, contenido),
    )
    if notificar_odoo:
        from odoo_crm import registrar_mensaje_historial
        asyncio.create_task(registrar_mensaje_historial(user_id, rol, contenido))


async def guardar_lead(user_id: str, resumen: str, canal: str = "instagram",
                       odoo_lead_id: int = 0) -> int:
    lead_id = await _run(
        "INSERT INTO leads (ig_user_id, canal, resumen, odoo_lead_id) VALUES (?, ?, ?, ?) RETURNING id",
        (user_id, canal, resumen, odoo_lead_id),
    )
    log.info("Lead guardado — user=%s canal=%s odoo_id=%s", user_id, canal, odoo_lead_id)
    return lead_id


async def tiene_lead_activo(user_id: str) -> bool:
    """True si hay un lead creado en las últimas 2 horas (misma sesión)."""
    rows = await _query(
        """SELECT id FROM leads WHERE ig_user_id = ? AND odoo_lead_id > 0
           AND creado_en > now() - interval '2 hours'""",
        (user_id,)
    )
    return bool(rows)


async def cerrar_conversacion(user_id: str):
    await _run("UPDATE usuarios SET cerrada = 1 WHERE ig_user_id = ?", (user_id,))


async def resetear_cerrada(user_id: str):
    await _run("UPDATE usuarios SET cerrada = 0 WHERE ig_user_id = ?", (user_id,))


async def limpiar_historial(user_id: str):
    await _run("DELETE FROM historial WHERE ig_user_id = ?", (user_id,))


async def obtener_canonical_id(user_id: str) -> str:
    """Devuelve el canonical_id si el usuario está vinculado, sino el mismo user_id."""
    rows = await _query(
        "SELECT canonical_id FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    if rows and rows[0].get("canonical_id"):
        return rows[0]["canonical_id"]
    return user_id


async def actualizar_ultima_orden(user_id: str, order_id: int, nro_orden: str = ""):
    """Guarda la última orden notificada a este cliente (id numérico + número
    legible, ej. "2026-10581"), para que los mensajes posteriores (ej.
    comprobantes de pago) se registren también en el chatter de esa orden en
    Odoo, y para que el bot pueda responder consultas de estado sin pedirle
    de nuevo el número si el cliente ya lo tiene notificado.

    Usa upsert (no UPDATE) porque este es a veces el primer contacto con un
    cliente nuevo — Odoo puede notificar "orden confirmada" antes de que el
    cliente le haya escrito nunca a BridgeBot, así que su fila en `usuarios`
    todavía no existe."""
    await _run(
        """INSERT INTO usuarios (ig_user_id, ultima_orden_id, ultima_orden_nro) VALUES (?, ?, ?)
           ON CONFLICT(ig_user_id) DO UPDATE SET ultima_orden_id = ?, ultima_orden_nro = ?""",
        (user_id, order_id, nro_orden, order_id, nro_orden),
    )


async def obtener_ultima_orden(user_id: str) -> int:
    """Devuelve el order_id de la última orden notificada a este cliente, o 0 si
    no hay ninguna (o el mensaje llegó antes de cualquier notificación de orden)."""
    rows = await _query(
        "SELECT ultima_orden_id FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    if rows and rows[0].get("ultima_orden_id"):
        return rows[0]["ultima_orden_id"]
    return 0


async def obtener_ultima_orden_nro(user_id: str) -> str:
    """Devuelve el número legible (ej. "2026-10581") de la última orden
    notificada a este cliente, o "" si no hay ninguna."""
    rows = await _query(
        "SELECT ultima_orden_nro FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    return rows[0]["ultima_orden_nro"] if rows and rows[0].get("ultima_orden_nro") else ""


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


async def detectar_duplicados_telefono() -> list[list[dict]]:
    """Agrupa usuarios de WhatsApp cuyo número (últimos 10 dígitos) coincide
    pero con ig_user_id distinto — señal de que es la misma persona guardada
    con formato de teléfono distinto (con/sin código de país, etc.)."""
    grupos = await _query(
        """SELECT right(ig_user_id, 10) as sufijo
           FROM usuarios
           WHERE canal = 'whatsapp' AND length(ig_user_id) >= 10
           GROUP BY sufijo
           HAVING COUNT(*) > 1"""
    )
    resultado = []
    for g in grupos:
        filas = await _query(
            """SELECT u.ig_user_id, u.nombre, u.telefono,
                      (SELECT COUNT(*) FROM historial h WHERE h.ig_user_id = u.ig_user_id) as mensajes,
                      (SELECT MAX(creado_en) FROM historial h WHERE h.ig_user_id = u.ig_user_id) as ultimo_mensaje
               FROM usuarios u
               WHERE u.canal = 'whatsapp' AND right(u.ig_user_id, 10) = ?
               ORDER BY mensajes DESC""",
            (g["sufijo"],),
        )
        resultado.append(filas)
    return resultado


async def unificar_clientes(primario: str, duplicado: str):
    """Mueve todo el historial y archivos de `duplicado` a `primario`, completa
    los datos que falten en `primario` con los de `duplicado`, y borra el
    registro duplicado. Usar cuando dos ig_user_id son en realidad el mismo
    número de teléfono guardado con formato distinto."""
    if primario == duplicado:
        return
    await _run("UPDATE historial SET ig_user_id = ? WHERE ig_user_id = ?", (primario, duplicado))
    await _run("UPDATE archivos  SET ig_user_id = ? WHERE ig_user_id = ?", (primario, duplicado))

    filas_dup = await _query("SELECT nombre, telefono, email FROM usuarios WHERE ig_user_id = ?", (duplicado,))
    filas_base = await _query("SELECT nombre, telefono, email FROM usuarios WHERE ig_user_id = ?", (primario,))
    if filas_dup:
        dup, base = filas_dup[0], (filas_base[0] if filas_base else {})
        sets, vals = [], []
        for campo in ("nombre", "telefono", "email"):
            if not base.get(campo) and dup.get(campo):
                sets.append(f"{campo} = ?")
                vals.append(dup[campo])
        if sets:
            vals.append(primario)
            await _run(f"UPDATE usuarios SET {', '.join(sets)} WHERE ig_user_id = ?", tuple(vals))

    await _run("DELETE FROM usuarios WHERE ig_user_id = ?", (duplicado,))
    log.info("Clientes unificados: %s -> %s", duplicado, primario)


async def guardar_datos_cliente(user_id: str, nombre: str = "", telefono: str = "", email: str = ""):
    sets, vals = [], []
    if nombre:
        sets.append("nombre = ?");   vals.append(nombre)
    if telefono:
        sets.append("telefono = ?"); vals.append(telefono)
    if email:
        sets.append("email = ?");    vals.append(email)
    if sets:
        await _run(f"UPDATE usuarios SET {', '.join(sets)} WHERE ig_user_id = ?", (*vals, user_id))


async def obtener_datos_cliente(user_id: str) -> dict:
    rows = await _query(
        "SELECT nombre, telefono, email, canal FROM usuarios WHERE ig_user_id = ?", (user_id,)
    )
    if rows:
        return {
            "nombre":   rows[0].get("nombre")   or "",
            "telefono": rows[0].get("telefono") or "",
            "email":    rows[0].get("email")    or "",
            "canal":    rows[0].get("canal")    or "",
        }
    return {"nombre": "", "telefono": "", "email": "", "canal": ""}


async def conversacion_cerrada(user_id: str) -> bool:
    canonical = await obtener_canonical_id(user_id)
    rows = await _query(
        "SELECT cerrada FROM usuarios WHERE ig_user_id = ? AND cerrada = 1", (canonical,)
    )
    return bool(rows)


async def pausar_usuario(user_id: str):
    """Pausa el bot para este usuario (atención manual) hasta que se reanude."""
    canonical = await obtener_canonical_id(user_id)
    await _run("UPDATE usuarios SET pausado = 1 WHERE ig_user_id = ?", (canonical,))


async def reanudar_usuario(user_id: str):
    canonical = await obtener_canonical_id(user_id)
    await _run("UPDATE usuarios SET pausado = 0 WHERE ig_user_id = ?", (canonical,))


async def usuario_pausado(user_id: str) -> bool:
    canonical = await obtener_canonical_id(user_id)
    rows = await _query(
        "SELECT pausado FROM usuarios WHERE ig_user_id = ? AND pausado = 1", (canonical,)
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


async def buscar_en_historial(texto: str, limite: int = 50) -> list[dict]:
    """Usuarios cuyas conversaciones contienen el texto buscado."""
    return await _query(
        """SELECT DISTINCT h.ig_user_id, u.nombre, u.telefono, u.canal,
                  MAX(h.creado_en) as ultimo_mensaje, h.contenido as ultimo_texto,
                  'user' as ultimo_rol
           FROM historial h
           LEFT JOIN usuarios u ON u.ig_user_id = h.ig_user_id
           WHERE h.rol = 'user' AND LOWER(h.contenido) LIKE LOWER(?)
           GROUP BY h.ig_user_id
           ORDER BY ultimo_mensaje DESC
           LIMIT ?""",
        (f"%{texto}%", limite),
    )


async def obtener_conversaciones_recientes(limite: int = 20, offset: int = 0, canal: str = "") -> list[dict]:
    """Últimas conversaciones (una fila por cliente), paginadas por mensaje más reciente.
    Incluye conversaciones sin mensajes del cliente (ej. notificaciones salientes de Odoo
    a alguien que nunca escribió) — no filtra por rol. `canal` filtra por 'whatsapp'/'instagram'."""
    where = "WHERE u.canal = ?" if canal else ""
    params = (canal, limite, offset) if canal else (limite, offset)
    return await _query(
        f"""SELECT h.ig_user_id, u.nombre, u.telefono, u.canal,
                  h.creado_en as ultimo_mensaje, h.contenido as ultimo_texto, h.rol as ultimo_rol
           FROM historial h
           JOIN (SELECT ig_user_id, MAX(id) as max_id FROM historial GROUP BY ig_user_id) ult
                ON ult.max_id = h.id
           LEFT JOIN usuarios u ON u.ig_user_id = h.ig_user_id
           {where}
           ORDER BY h.creado_en DESC
           LIMIT ? OFFSET ?""",
        params,
    )


async def guardar_archivo(user_id: str, canal: str, tipo: str,
                          media_id: str = "", url: str = "") -> int:
    return await _run(
        "INSERT INTO archivos (ig_user_id, canal, tipo, media_id, url) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (user_id, canal, tipo, media_id, url),
    )


async def marcar_comprobante(archivo_id: int, datos_json: str):
    """Marca un archivo como comprobante de pago detectado, con los datos extraídos (JSON)."""
    await _run(
        "UPDATE archivos SET es_comprobante = 1, datos_comprobante = ? WHERE id = ?",
        (datos_json, archivo_id),
    )


async def obtener_archivos(user_id: str) -> list[dict]:
    return await _query(
        "SELECT tipo, media_id, url, creado_en FROM archivos WHERE ig_user_id = ? ORDER BY id ASC",
        (user_id,),
    )


async def obtener_archivo_por_id(archivo_id: int) -> dict | None:
    rows = await _query(
        """SELECT a.id, a.ig_user_id, a.canal, a.tipo, a.media_id, a.url, a.creado_en,
                  a.es_comprobante, a.datos_comprobante, u.nombre, u.telefono
           FROM archivos a
           LEFT JOIN usuarios u ON u.ig_user_id = a.ig_user_id
           WHERE a.id = ?""",
        (archivo_id,),
    )
    return rows[0] if rows else None


async def listar_archivos(limite: int = 200) -> list[dict]:
    return await _query(
        """SELECT a.id, a.ig_user_id, a.canal, a.tipo, a.media_id, a.url, a.creado_en,
                  a.es_comprobante, a.datos_comprobante, u.nombre, u.telefono
           FROM archivos a
           LEFT JOIN usuarios u ON u.ig_user_id = a.ig_user_id
           ORDER BY a.id DESC LIMIT ?""",
        (limite,),
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
               VALUES (?, ?, ?, ?, now())
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


async def listar_clientes_odoo(q: str = "", limite: int = 50, offset: int = 0) -> list[dict]:
    """Lista paginada de clientes sincronizados de Odoo, con búsqueda opcional
    por nombre o teléfono. Es de solo lectura — la sync nocturna es la fuente de verdad."""
    if q:
        return await _query(
            """SELECT odoo_id, nombre, telefono, email, synced_at FROM clientes_odoo
               WHERE nombre LIKE ? OR telefono LIKE ?
               ORDER BY LOWER(nombre) ASC
               LIMIT ? OFFSET ?""",
            (f"%{q}%", f"%{q}%", limite, offset),
        )
    return await _query(
        """SELECT odoo_id, nombre, telefono, email, synced_at FROM clientes_odoo
           ORDER BY LOWER(nombre) ASC
           LIMIT ? OFFSET ?""",
        (limite, offset),
    )


async def contar_clientes_odoo() -> int:
    rows = await _query("SELECT COUNT(*) as n FROM clientes_odoo")
    return rows[0]["n"] if rows else 0


async def buscar_cliente_odoo_por_id(odoo_id: int) -> dict | None:
    rows = await _query(
        "SELECT odoo_id, nombre, telefono, email FROM clientes_odoo WHERE odoo_id = ?",
        (odoo_id,),
    )
    return rows[0] if rows else None


async def mensaje_ya_procesado(message_id: str) -> bool:
    rows = await _query(
        "SELECT 1 FROM mensajes_procesados WHERE message_id = ?", (message_id,)
    )
    return bool(rows)


async def marcar_mensaje_procesado(message_id: str):
    await _run(
        "INSERT INTO mensajes_procesados (message_id) VALUES (?) ON CONFLICT DO NOTHING",
        (message_id,),
    )


async def upsert_tareas_odoo(tareas: list[dict]):
    """Bulk upsert de tareas desde Odoo."""
    if not tareas:
        return
    statements = [
        (
            """INSERT INTO tareas_odoo
                   (odoo_id, task_name, nro_orden, stage, partner_name,
                    telefono, documento, sale_order_name, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
               ON CONFLICT(odoo_id) DO UPDATE SET
                   stage=excluded.stage,
                   partner_name=excluded.partner_name,
                   telefono=excluded.telefono,
                   documento=excluded.documento,
                   synced_at=excluded.synced_at""",
            (
                t["odoo_id"], t["task_name"], t["nro_orden"], t["stage"],
                t["partner_name"], t["telefono"], t["documento"], t["sale_order_name"],
            ),
        )
        for t in tareas
    ]
    await _batch_run(statements)
    log.info("Sync tareas Odoo: %d actualizadas en DB local", len(tareas))


async def buscar_tareas_por_telefono(telefono: str) -> list[dict]:
    """Busca tareas del cliente por los últimos 10 dígitos del teléfono."""
    digitos = "".join(c for c in telefono if c.isdigit())
    sufijo  = digitos[-10:] if len(digitos) >= 10 else digitos
    if not sufijo:
        return []
    return await _query(
        """SELECT task_name, nro_orden, stage, partner_name, sale_order_name
           FROM tareas_odoo
           WHERE telefono LIKE ?
           ORDER BY odoo_id DESC
           LIMIT 5""",
        (f"%{sufijo}",),
    )


async def buscar_tareas_por_nro_orden(nro: str) -> list[dict]:
    """Busca tareas por número de orden exacto o por coincidencia en task_name."""
    if not nro:
        return []
    return await _query(
        """SELECT task_name, nro_orden, stage, partner_name, sale_order_name
           FROM tareas_odoo
           WHERE nro_orden = ? OR task_name LIKE ?
           ORDER BY odoo_id DESC
           LIMIT 5""",
        (nro, f"%{nro}%"),
    )


async def buscar_tareas_por_nombre(nombre: str) -> list[dict]:
    """Busca tareas por nombre del cliente (búsqueda parcial, case-insensitive)."""
    if not nombre or len(nombre.strip()) < 3:
        return []
    return await _query(
        """SELECT task_name, nro_orden, stage, partner_name, sale_order_name
           FROM tareas_odoo
           WHERE LOWER(partner_name) LIKE LOWER(?)
              OR LOWER(task_name)    LIKE LOWER(?)
           ORDER BY odoo_id DESC
           LIMIT 5""",
        (f"%{nombre.strip()}%", f"%{nombre.strip()}%"),
    )


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


# ─── CONFIGURACIÓN (panel de admin) ───────────────────────────────────────────

async def obtener_config(clave: str) -> str | None:
    """Override guardado desde el panel de configuración, o None si no existe."""
    rows = await _query("SELECT valor FROM configuracion WHERE clave = ?", (clave,))
    return rows[0]["valor"] if rows else None


async def obtener_config_todas() -> dict[str, str]:
    rows = await _query("SELECT clave, valor FROM configuracion")
    return {r["clave"]: r["valor"] for r in rows}


async def guardar_config(clave: str, valor: str):
    await _run(
        """INSERT INTO configuracion (clave, valor, actualizado_en) VALUES (?, ?, now())
           ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, actualizado_en = excluded.actualizado_en""",
        (clave, valor),
    )
