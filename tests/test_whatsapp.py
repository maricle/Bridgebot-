"""
Tests de parsing de payloads de WhatsApp Business.

Cubre los casos observados en producción:
- Extracción correcta del message_id (wamid.*) para deduplicación
- Parsing de mensajes de texto
- Parsing de archivos de imagen/documento enviados por el cliente
- Payloads vacíos o sin media → no rompen el sistema
"""

from whatsapp import extraer_archivos, extraer_mensaje, extraer_message_id

# ─── Payloads base ────────────────────────────────────────────────────────────

_WAMID = "wamid.HBgNNTQ5MzcwNDc2Njc0MBUCABIYFjNFQjBFQTI5MzM4NTQ2NDkwOTlDAA=="
_SENDER = "5493704766740"


def _payload_texto(sender: str = _SENDER, texto: str = "Hola", mid: str = _WAMID) -> dict:
    return {
        "entry": [{
            "id": "PHONE_NUMBER_ID",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "messages": [{
                        "from": sender,
                        "id": mid,
                        "timestamp": "1735000000",
                        "text": {"body": texto},
                        "type": "text",
                    }],
                },
                "field": "messages",
            }],
        }]
    }


def _payload_imagen(sender: str = _SENDER, media_id: str = "media_img_001",
                    mid: str = "wamid.img001") -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": sender,
                        "id": mid,
                        "type": "image",
                        "image": {
                            "id": media_id,
                            "mime_type": "image/jpeg",
                            "sha256": "abc123",
                        },
                    }]
                },
                "field": "messages",
            }]
        }]
    }


def _payload_documento(sender: str = _SENDER, media_id: str = "media_doc_001",
                        mid: str = "wamid.doc001") -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": sender,
                        "id": mid,
                        "type": "document",
                        "document": {
                            "id": media_id,
                            "mime_type": "application/pdf",
                            "filename": "diseño.pdf",
                        },
                    }]
                },
                "field": "messages",
            }]
        }]
    }


# ─── extraer_message_id ───────────────────────────────────────────────────────

def test_extraer_message_id_texto():
    mid = extraer_message_id(_payload_texto())
    assert mid == _WAMID


def test_extraer_message_id_imagen():
    mid = extraer_message_id(_payload_imagen())
    assert mid == "wamid.img001"


def test_extraer_message_id_vacio():
    assert extraer_message_id({}) == ""


def test_extraer_message_id_sin_mensajes():
    payload = {"entry": [{"changes": [{"value": {"messages": []}, "field": "messages"}]}]}
    assert extraer_message_id(payload) == ""


# ─── extraer_mensaje ──────────────────────────────────────────────────────────

def test_extraer_mensaje_texto():
    sender, texto = extraer_mensaje(_payload_texto(texto="Necesito 10 copias A3"))
    assert sender == _SENDER
    assert texto == "Necesito 10 copias A3"


def test_extraer_mensaje_pinterest():
    """Row 145 del DB: usuario envía link de Pinterest como texto."""
    sender, texto = extraer_mensaje(
        _payload_texto(texto="Take a look! 👀 https://pin.it/2hgK6XPaM")
    )
    assert sender == _SENDER
    assert "https://pin.it/2hgK6XPaM" in texto


def test_extraer_mensaje_payload_imagen_retorna_vacio():
    """Un payload de imagen no tiene texto → extraer_mensaje devuelve vacío."""
    sender, texto = extraer_mensaje(_payload_imagen())
    assert sender == ""
    assert texto == ""


def test_extraer_mensaje_payload_vacio():
    sender, texto = extraer_mensaje({})
    assert sender == "" and texto == ""


# ─── extraer_archivos ─────────────────────────────────────────────────────────

def test_extraer_archivos_imagen():
    sender, archivos = extraer_archivos(_payload_imagen(media_id="media_img_001"))
    assert sender == _SENDER
    assert len(archivos) == 1
    assert archivos[0]["tipo"] == "image"
    assert archivos[0]["media_id"] == "media_img_001"


def test_extraer_archivos_documento():
    sender, archivos = extraer_archivos(_payload_documento(media_id="media_doc_001"))
    assert sender == _SENDER
    assert len(archivos) == 1
    assert archivos[0]["tipo"] == "document"
    assert archivos[0]["media_id"] == "media_doc_001"


def test_extraer_archivos_en_texto_retorna_vacio():
    """Mensaje de texto no es archivo → no debe tratarse como archivo."""
    sender, archivos = extraer_archivos(_payload_texto())
    assert archivos == []


def test_extraer_archivos_payload_vacio():
    sender, archivos = extraer_archivos({})
    assert sender == "" and archivos == []
