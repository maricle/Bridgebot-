def solo_digitos(texto: str) -> str:
    """Extrae solo los dígitos de un string — normaliza teléfonos con
    distinto formato (espacios, guiones, prefijo +54, etc.)."""
    return "".join(c for c in (texto or "") if c.isdigit())


def sufijo_telefono(telefono: str, n: int = 10) -> str:
    """Últimos `n` dígitos de un teléfono — ignora prefijos de país y
    formato, para poder matchear el mismo número guardado de formas distintas."""
    digitos = solo_digitos(telefono)
    return digitos[-n:] if len(digitos) >= n else digitos
