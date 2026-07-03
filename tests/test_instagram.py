"""
Tests de parsing de payloads de Instagram.

Cubre los casos observados en producción:
- Extracción del mid (message ID de Instagram) para deduplicación
- Parsing de mensajes de texto
- Archivos con URL → se extraen correctamente
- Archivos sin URL en payload → se descartan (no causan errores)
  (Patrón real: usuarios de IG envían imágenes que a veces llegan sin URL en el payload)
"""

from instagram import extraer_archivos, extraer_mensaje, extraer_message_id

# ─── Payloads base ────────────────────────────────────────────────────────────

_MID   = "m_ABCdefGHI123xyz"
_SENDER = "5493705137369"
_PAGE   = "17841456843060136"


def _payload_texto(sender: str = _SENDER, texto: str = "Hola IG",
                   mid: str = _MID) -> dict:
    return {
        "entry": [{
            "id": _PAGE,
            "time": 1735000000,
            "messaging": [{
                "sender":    {"id": sender},
                "recipient": {"id": _PAGE},
                "timestamp": 1735000000,
                "message":   {"mid": mid, "text": texto},
            }],
        }]
    }


def _payload_imagen_con_url(sender: str = _SENDER,
                             mid: str = "m_img001",
                             url: str = "https://cdn.instagram.com/img123.jpg") -> dict:
    return {
        "entry": [{
            "messaging": [{
                "sender":  {"id": sender},
                "message": {
                    "mid": mid,
                    "attachments": [{
                        "type":    "image",
                        "payload": {"url": url},
                    }],
                },
            }]
        }]
    }


def _payload_imagen_sin_url(sender: str = _SENDER, mid: str = "m_nourl001") -> dict:
    """Caso real: IG a veces envía attachments sin URL (imagen efímera / sticker)."""
    return {
        "entry": [{
            "messaging": [{
                "sender":  {"id": sender},
                "message": {
                    "mid": mid,
                    "attachments": [{"type": "image", "payload": {}}],
                },
            }]
        }]
    }


def _payload_burst(sender: str = _SENDER, links: list[str] | None = None) -> list[dict]:
    """Simula ráfaga de mensajes — patrón del usuario 5493705137369 (filas 389-413 del DB)."""
    links = links or [
        "https://pin.it/2hgK6XPaM",
        "https://pin.it/5H5RtMrLT",
        "https://pin.it/3AbCdEfGh",
    ]
    return [
        _payload_texto(sender=sender, texto=link, mid=f"m_burst_{i}")
        for i, link in enumerate(links)
    ]


# ─── extraer_message_id ───────────────────────────────────────────────────────

def test_extraer_message_id_texto():
    assert extraer_message_id(_payload_texto()) == _MID


def test_extraer_message_id_imagen():
    assert extraer_message_id(_payload_imagen_con_url()) == "m_img001"


def test_extraer_message_id_vacio():
    assert extraer_message_id({}) == ""


def test_extraer_message_id_burst_cada_mensaje_tiene_id_unico():
    """Cada mensaje del burst debe tener su propio mid único."""
    payloads = _payload_burst()
    mids = [extraer_message_id(p) for p in payloads]
    assert len(mids) == len(set(mids)), "Los mid del burst no son únicos"


# ─── extraer_mensaje ──────────────────────────────────────────────────────────

def test_extraer_mensaje_texto():
    sender, texto = extraer_mensaje(_payload_texto(texto="Necesito lona 3x2"))
    assert sender == _SENDER
    assert texto == "Necesito lona 3x2"


def test_extraer_mensaje_pinterest():
    """Usuario envía link Pinterest como texto — patrón de filas 389-413 del DB."""
    sender, texto = extraer_mensaje(
        _payload_texto(texto="https://pin.it/5H5RtMrLT")
    )
    assert sender == _SENDER
    assert "https://pin.it/5H5RtMrLT" in texto


def test_extraer_mensaje_payload_imagen_retorna_vacio():
    sender, texto = extraer_mensaje(_payload_imagen_con_url())
    assert sender == "" and texto == ""


def test_extraer_mensaje_vacio():
    sender, texto = extraer_mensaje({})
    assert sender == "" and texto == ""


# ─── extraer_archivos ─────────────────────────────────────────────────────────

def test_extraer_archivos_imagen_con_url():
    sender, archivos = extraer_archivos(_payload_imagen_con_url(
        url="https://cdn.instagram.com/img123.jpg"
    ))
    assert sender == _SENDER
    assert len(archivos) == 1
    assert archivos[0]["tipo"] == "image"
    assert archivos[0]["url"] == "https://cdn.instagram.com/img123.jpg"


def test_extraer_archivos_sin_url_se_descarta():
    """
    Caso real (filas 437-440 del DB): IG envía attachment sin URL.
    El bot no debe crashear ni pedir el archivo de nuevo — simplemente lo ignora.
    """
    sender, archivos = extraer_archivos(_payload_imagen_sin_url())
    # sin URL válida → no se incluye en la lista
    assert archivos == []


def test_extraer_archivos_texto_retorna_vacio():
    sender, archivos = extraer_archivos(_payload_texto())
    assert archivos == []


def test_extraer_archivos_vacio():
    sender, archivos = extraer_archivos({})
    assert sender == "" and archivos == []
