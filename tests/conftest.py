import os

# Set before any BridgeBot module import so config.py reads these values
os.environ.setdefault("META_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("META_APP_SECRET",   "test_app_secret_32chars_padded00")
os.environ.setdefault("IG_ACCESS_TOKEN",   "test_ig_token")
os.environ.setdefault("WA_ACCESS_TOKEN",   "test_wa_token")
os.environ.setdefault("WA_PHONE_ID",       "123456789")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("MODO_DEV",          "true")
os.environ.setdefault("SKIP_FIRMA",        "true")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://bridgebot:bridgebot@localhost:5432/bridgebot_test",
)

import asyncpg
import pytest

import db as _db
from config import DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
async def init_test_db():
    # Schema limpio por sesión de pytest — evita el problema (real, sufrido esta
    # sesión) de estado que persiste entre corridas y deja IDs/teléfonos hardcodeados
    # marcados como "ya usados" para siempre. Requiere un Postgres real alcanzable
    # en DATABASE_URL (ver docker-compose.yml) — sin fallback silencioso.
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    await _db.init_db()
