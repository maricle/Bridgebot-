"""
Tests de la capa de DB (SQLite local en tests).

Cubre los patrones de falla del historial de producción:

1. Deduplicación de mensajes (mensajes_procesados):
   - Meta reintenta webhooks → mismo wamid procesado dos veces → segundo se ignora
   - Usuario 5493705137369 mandó ráfaga de 10 links → múltiples reintentos de Meta

2. Persistencia de archivos (tabla archivos):
   - WA imagen (media_id) → guardada y recuperada correctamente
   - IG imagen (url)      → guardada y recuperada correctamente

3. Búsqueda de clientes Odoo por teléfono:
   - Número con prefijo internacional (+54 9 11...) → encontrado por últimos 10 dígitos

Nota: todos los IDs usan uuid4 para evitar colisiones entre corridas de pytest.
"""

import uuid

import db


def uid() -> str:
    return str(uuid.uuid4())


# ─── Deduplicación de mensajes ───────────────────────────────────────────────

async def test_dedup_mensaje_nuevo_retorna_false():
    result = await db.mensaje_ya_procesado(f"wamid.NUEVO_{uid()}")
    assert result is False


async def test_dedup_despues_de_marcar_retorna_true():
    mid = f"wamid.MARK_{uid()}"
    await db.marcar_mensaje_procesado(mid)
    assert await db.mensaje_ya_procesado(mid) is True


async def test_dedup_idempotente_no_lanza_error():
    """INSERT OR IGNORE: marcar el mismo mid dos veces no debe lanzar excepción."""
    mid = f"wamid.IDEM_{uid()}"
    await db.marcar_mensaje_procesado(mid)
    await db.marcar_mensaje_procesado(mid)
    assert await db.mensaje_ya_procesado(mid) is True


async def test_dedup_ids_independientes():
    """Dos mensajes distintos → estados independientes."""
    mid_a = f"wamid.A_{uid()}"
    mid_b = f"wamid.B_{uid()}"
    await db.marcar_mensaje_procesado(mid_a)
    assert await db.mensaje_ya_procesado(mid_a) is True
    assert await db.mensaje_ya_procesado(mid_b) is False


async def test_dedup_rafaga_meta_reintentos():
    """
    Simula el patrón de filas 389-413: Meta reintenta 3 veces el mismo webhook.
    Solo el primero debe procesarse; los siguientes ya están marcados.
    """
    mid = f"wamid.BURST_PINTEREST_{uid()}"
    # Primer intento → no procesado aún
    assert await db.mensaje_ya_procesado(mid) is False

    # Procesamos (marcamos)
    await db.marcar_mensaje_procesado(mid)

    # Reintentos 2 y 3 de Meta → ya procesado
    assert await db.mensaje_ya_procesado(mid) is True
    assert await db.mensaje_ya_procesado(mid) is True


async def test_dedup_ig_mid():
    """La deduplicación también aplica a mensajes de Instagram (mid en vez de wamid)."""
    mid = f"m_IG_{uid()}"
    assert await db.mensaje_ya_procesado(mid) is False
    await db.marcar_mensaje_procesado(mid)
    assert await db.mensaje_ya_procesado(mid) is True


# ─── Persistencia de archivos ─────────────────────────────────────────────────

async def test_guardar_y_obtener_archivo_wa_imagen():
    """
    Cuando un cliente de WA envía una imagen, se guarda con media_id.
    Esto cubre el patrón de 'pedido de archivo dos veces': si el archivo
    ya está en DB, el sistema sabe que fue recibido.
    """
    user_id = f"wa_img_{uid()}"
    media_id = f"media_img_{uid()}"
    await db.guardar_archivo(user_id, "whatsapp", "image", media_id=media_id)

    archivos = await db.obtener_archivos(user_id)
    assert len(archivos) == 1
    assert archivos[0]["tipo"] == "image"
    assert archivos[0]["media_id"] == media_id


async def test_guardar_y_obtener_archivo_wa_documento():
    user_id  = f"wa_doc_{uid()}"
    media_id = f"media_doc_{uid()}"
    await db.guardar_archivo(user_id, "whatsapp", "document", media_id=media_id)

    archivos = await db.obtener_archivos(user_id)
    assert len(archivos) == 1
    assert archivos[0]["tipo"] == "document"


async def test_guardar_y_obtener_archivo_ig_imagen():
    """IG envía URL en vez de media_id."""
    user_id = f"ig_img_{uid()}"
    url = f"https://cdn.instagram.com/img_{uid()}.jpg"
    await db.guardar_archivo(user_id, "instagram", "image", url=url)

    archivos = await db.obtener_archivos(user_id)
    assert len(archivos) == 1
    assert archivos[0]["url"] == url


async def test_usuario_sin_archivos_retorna_lista_vacia():
    archivos = await db.obtener_archivos(f"usuario_sin_archivos_{uid()}")
    assert archivos == []


async def test_multiples_archivos_mismo_usuario():
    user_id = f"multi_arch_{uid()}"
    await db.guardar_archivo(user_id, "whatsapp", "image",    media_id=f"m_img_{uid()}")
    await db.guardar_archivo(user_id, "whatsapp", "document", media_id=f"m_doc_{uid()}")

    archivos = await db.obtener_archivos(user_id)
    assert len(archivos) == 2
    tipos = {a["tipo"] for a in archivos}
    assert "image" in tipos and "document" in tipos


# ─── Búsqueda de clientes Odoo por teléfono ──────────────────────────────────

async def test_buscar_cliente_odoo_por_sufijo_10_digitos():
    """
    El número en WA llega como '5493704766740' (con prefijo de país).
    En Odoo puede estar guardado como '3704766740' (sin 549).
    La búsqueda debe funcionar por los últimos 10 dígitos.
    """
    odoo_id = int(uid().replace("-", "")[:6], 16) % 900000 + 100000
    await db.upsert_clientes_odoo([{
        "odoo_id": odoo_id,
        "nombre":  "Ana García",
        "telefono": "3704766740",
        "email":   "ana@example.com",
    }])

    result = await db.buscar_cliente_odoo_por_telefono("5493704766740")
    assert result is not None
    assert result["nombre"] == "Ana García"


async def test_buscar_cliente_odoo_numero_no_encontrado():
    result = await db.buscar_cliente_odoo_por_telefono(f"549{uid().replace('-','')[:10]}")
    assert result is None


async def test_buscar_cliente_odoo_telefono_vacio():
    result = await db.buscar_cliente_odoo_por_telefono("")
    assert result is None
