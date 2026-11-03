import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "correct horse battery staple"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Clean environment pointing the warehouse at a temp folder."""
    for k in list(os.environ):
        if k.startswith(("RISK_", "STRIPE_", "AWS_", "AZURE_", "SHAREPOINT_", "PAYMENT_", "WEB_", "S3_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RISK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RISK_ENCRYPTION_PASSPHRASE", PASS)
    return tmp_path


@pytest.fixture
def warehouse(env):
    from rm import config, db
    s = config.get_settings()
    conn = db.connect(s.risk_db)
    db.init_db(conn, s.risk_passphrase)
    yield conn
    conn.close()
