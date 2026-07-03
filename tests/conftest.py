import os
import tempfile

# Set before any BridgeBot module import so config.py reads these values
_tmp_db = os.path.join(tempfile.gettempdir(), "bridgebot_test.db")

os.environ.setdefault("META_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("META_APP_SECRET",   "test_app_secret_32chars_padded00")
os.environ.setdefault("IG_ACCESS_TOKEN",   "test_ig_token")
os.environ.setdefault("WA_ACCESS_TOKEN",   "test_wa_token")
os.environ.setdefault("WA_PHONE_ID",       "123456789")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("TURSO_URL",         "")
os.environ.setdefault("TURSO_TOKEN",       "")
os.environ.setdefault("MODO_DEV",          "true")
os.environ.setdefault("SKIP_FIRMA",        "true")
os.environ["DB_PATH"] = _tmp_db

import pytest
import db as _db


@pytest.fixture(scope="session", autouse=True)
async def init_test_db():
    await _db.init_db()
