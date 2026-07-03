import logging

from config import PALABRAS_PRECIO, areas_categorias
from db import _query

log = logging.getLogger(__name__)


async def obtener_analytics(desde: str, hasta: str) -> dict:
    # Clientes nuevos por día (registrados en el período)
    rows_dias = await _query(
        """SELECT DATE(creado_en) as dia, COUNT(*) as total
           FROM usuarios
           WHERE DATE(creado_en) BETWEEN ? AND ?
           GROUP BY dia ORDER BY dia""",
        (desde, hasta),
    )

    # Total clientes activos (usuarios que enviaron al menos un mensaje)
    rows_activos = await _query(
        """SELECT COUNT(DISTINCT ig_user_id) as n
           FROM historial
           WHERE rol = 'user' AND DATE(creado_en) BETWEEN ? AND ?""",
        (desde, hasta),
    )

    # Mensajes de clientes para análisis de palabras clave
    rows_msgs = await _query(
        """SELECT ig_user_id, contenido
           FROM historial
           WHERE rol = 'user' AND DATE(creado_en) BETWEEN ? AND ?""",
        (desde, hasta),
    )

    # Leads generados en el período
    rows_leads = await _query(
        """SELECT COUNT(*) as n FROM leads
           WHERE DATE(creado_en) BETWEEN ? AND ?""",
        (desde, hasta),
    )

    total_clientes = int(rows_activos[0]["n"]) if rows_activos else 0
    total_leads    = int(rows_leads[0]["n"])   if rows_leads    else 0

    productos_config  = areas_categorias()
    usuarios_precio   = set()
    usuarios_producto = {cat: set() for cat in productos_config}

    for r in rows_msgs:
        texto = r["contenido"].lower()
        if any(k in texto for k in PALABRAS_PRECIO):
            usuarios_precio.add(r["ig_user_id"])
        for cat, keywords in productos_config.items():
            if any(k in texto for k in keywords):
                usuarios_producto[cat].add(r["ig_user_id"])

    productos = sorted(
        [{"producto": cat, "clientes": len(uids)}
         for cat, uids in usuarios_producto.items() if uids],
        key=lambda x: x["clientes"],
        reverse=True,
    )

    pct_precio = round(len(usuarios_precio) / total_clientes * 100, 1) if total_clientes else 0

    return {
        "desde":           desde,
        "hasta":           hasta,
        "total_clientes":  total_clientes,
        "total_leads":     total_leads,
        "clientes_por_dia": [
            {"dia": r["dia"], "total": int(r["total"])} for r in rows_dias
        ],
        "precio": {
            "cantidad":    len(usuarios_precio),
            "porcentaje":  pct_precio,
        },
        "productos": productos,
    }
