"""Detección y extracción de datos de comprobantes de pago en PDF.

Solo cubre PDFs con capa de texto real (los que generan la mayoría de los
bancos/billeteras al exportar un comprobante) — un PDF escaneado/foto sin
texto no se puede procesar con esto."""

import io
import json
import logging

log = logging.getLogger(__name__)

_LARGO_MINIMO = 15  # por debajo de esto, no vale la pena mandarlo a Claude


def extraer_texto_pdf(pdf_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        partes = [pagina.extract_text() or "" for pagina in reader.pages]
        return "\n".join(partes).strip()
    except Exception as e:
        log.error("Error extrayendo texto de PDF: %s", e)
        return ""


async def analizar_comprobante(texto: str) -> dict | None:
    """Le pide a Claude que determine si el texto es de un comprobante de pago
    y, si lo es, extraiga los datos principales. Devuelve None si no es un
    comprobante o si algo falla."""
    if len(texto.strip()) < _LARGO_MINIMO:
        return None

    from ai import _llamar_claude

    prompt = (
        "Este es el texto extraído de un PDF que un cliente mandó por WhatsApp.\n"
        "Decime si es un comprobante de pago o transferencia bancaria.\n\n"
        f"Texto:\n{texto[:3000]}\n\n"
        "Respondé ÚNICAMENTE con JSON válido, sin texto ni markdown alrededor, "
        "con este formato exacto:\n"
        '{"es_comprobante": true, "monto": "", "fecha": "", "destino": "", '
        '"operacion": "", "banco": ""}\n'
        "Si algún dato no aparece en el texto, dejalo como cadena vacía. "
        'Si NO es un comprobante de pago, respondé {"es_comprobante": false}.'
    )
    respuesta = await _llamar_claude([{"role": "user", "content": prompt}], max_tokens=300)
    if not respuesta:
        return None

    texto_json = respuesta.strip()
    if texto_json.startswith("```"):
        texto_json = texto_json.strip("`")
        if texto_json.startswith("json"):
            texto_json = texto_json[4:]
        texto_json = texto_json.strip()

    try:
        datos = json.loads(texto_json)
    except Exception as e:
        log.warning("No se pudo parsear la respuesta de Claude como JSON: %s — %r", e, respuesta[:200])
        return None

    if not datos.get("es_comprobante"):
        return None
    return datos
