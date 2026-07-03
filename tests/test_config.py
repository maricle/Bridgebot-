"""
Tests de detección de áreas y routing Odoo contra el knowledge/areas.json
real de Clever CNC (migrado desde el _detectar_flujo hardcodeado de
groq_ai.py a config.detectar_areas()).
"""

import config


def test_detectar_areas_laqueado():
    assert config.detectar_areas("necesito lacar una pieza") == ["laqueado"]


def test_detectar_areas_corte_cnc():
    areas = config.detectar_areas("quiero cotizar un corte cnc")
    assert "corte_cnc" in areas


def test_detectar_areas_sin_match():
    assert config.detectar_areas("hola, buenos días") == []


def test_resolver_destino_odoo_default():
    # Clever es single-company: ninguna área define "odoo_destino",
    # así que siempre debe caer al destino por defecto.
    areas = config.detectar_areas("necesito laqueado")
    assert config.resolver_destino_odoo(areas) == "default"


def test_resolver_destino_odoo_sin_areas():
    assert config.resolver_destino_odoo([]) == "default"
