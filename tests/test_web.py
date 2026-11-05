"""Dashboard + JSON API (FastAPI TestClient) on the fireworks scenario warehouse."""
import importlib.util
import json
import pathlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rm import config, db  # noqa: E402
from test_risk_signals import TODAY, scenario  # noqa: E402,F401

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def site(scenario, capsys):
    assert _load("risk_report", "bin/risk_report.py").main(["--no-email", "--today", TODAY]) == 0
    admin = _load("rm_admin", "bin/rm_admin.py")
    assert admin.main(["create-token", "Henry"]) == 0
    token = [l.strip() for l in capsys.readouterr().out.splitlines() if l.strip().startswith("tok_")][0]
    assert admin.main(["add-user", "risk.analyst@trybooking.co.uk", "--role", "viewer"]) == 0
    from api.app import create_app
    app = create_app(config.get_settings(), today=TODAY)
    return TestClient(app), token


def _signed_in(client, token):
    r = client.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client


def test_sign_in_required(site):
    client, token = site
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/api/risk").status_code == 401
    assert client.get("/api/health").json()["ok"] is True
    r = client.post("/login", data={"token": "tok_bad.nope"}, follow_redirects=False)
    assert "error=token" in r.headers["location"]
    r = client.post("/login", data={"token": token, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"                                      # no open redirect


def test_risk_pages(site):
    client, token = _signed_in(*site), site[1]
    page = client.get("/").text
    assert "Sparkle Events" in page and "P1" in page and "account created 01:30 (night)" in page
    page = client.get("/?level=event&priority=P1").text
    assert "Fireworks Spectacular" in page
    acc = client.get("/accounts/30").text
    for text in ("Sparkle Events", "Account created", "Bank transfers", "no bank transfers yet", "Fireworks Spectacular"):
        assert text in acc, text
    acc40 = client.get("/accounts/40").text
    assert "24 (£12,000)" in acc40 and "12 (£60,000)" in acc40
    ev = client.get("/events/300").text
    assert "Average ticket" in ev and "£100" in ev and "Payment attempts" in ev and "<svg" not in ev  # 1 run: no chart
    assert client.get("/accounts/999999").status_code == 404
    imports = client.get("/imports")
    assert imports.status_code == 200 and "s3:BookingData" in imports.text


def test_search(site):
    client, token = _signed_in(*site), site[1]
    r = client.get("/search?q=30", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accounts/30"
    assert "Village Bonfire" in client.get("/search?q=bonfire").text
    guid = "00000000-0000-4000-8000-000000003000"
    r = client.get(f"/search?q={guid.upper().replace('-', '')}", follow_redirects=False)
    assert r.headers["location"] == f"/bookings/{guid}"
    b = client.get(f"/bookings/{guid}").text
    assert "Fireworks Spectacular" in b and "Successful" in b and "jane@example.com" not in b
    found = client.get("/api/search", params={"q": "jane@example.com"}).json()
    assert found["kind"] == "email" and len(found["results"]) == 20              # 20 SharePoint rows with that email
    assert client.get("/api/search", params={"q": "VISA 4242"}).json()["kind"] == "card"
    assert client.get("/api/search", params={"q": "07700 900123"}).json()["kind"] == "phone"


def test_json_api_with_bearer_token(site):
    client, token = site
    h = {"Authorization": f"Bearer {token}"}
    risk = client.get("/api/risk?level=event&priority=P1", headers=h).json()
    assert risk["run"] == TODAY and [r["Id"] for r in risk["rows"]] == [300]
    acc = client.get("/api/accounts/40", headers=h).json()
    assert acc["transfers"]["count"] == 24 and acc["past"]["events"] == 12
    ev = client.get("/api/events/300", headers=h).json()
    assert ev["sales"]["FirstDay"] == 1000.0 and ev["latest"]["Priority"] == "P1"
    assert client.get("/api/events/1", headers=h).status_code == 404
    assert client.get("/api/imports", headers=h).status_code == 200
    assert client.get("/api/risk?priority=P9", headers=h).status_code == 400


def test_audit_log_has_no_search_terms(site):
    client, token = _signed_in(*site), site[1]
    client.get("/search", params={"q": "jane@example.com"})
    logs = list((pathlib.Path(config.get_settings().data_dir) / "logs").glob("web-audit-*.log"))
    text = logs[0].read_text()
    assert "jane@example.com" not in text and "jane" not in text
    lines = [json.loads(l) for l in text.splitlines()]
    s = [l for l in lines if l["path"] == "/search"][0]
    assert s["who"] == "token:Henry" and len(s["query"]) == 16


def test_security_headers_and_read_only(site):
    client, token = _signed_in(*site), site[1]
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src 'none'" in r.headers["Content-Security-Policy"]
    conn = db.connect(config.get_settings().risk_db, readonly=True)
    with pytest.raises(Exception):
        conn.execute("DELETE FROM accounts")
    conn.close()


class FakeEntra:
    def __init__(self, email):
        self.email = email

    def start(self):
        return {"auth_uri": "https://login.microsoftonline.com/x/authorize?state=s", "state": "s"}

    def finish(self, flow, params):
        assert params.get("state") == flow["state"]
        return self.email


@pytest.mark.parametrize("email, allowed", [("Risk.Analyst@trybooking.co.uk", True), ("someone@else.com", False)])
def test_microsoft_sign_in(site, email, allowed):
    from api.app import create_app
    client = TestClient(create_app(config.get_settings(), entra=FakeEntra(email.lower()), today=TODAY))
    assert "Sign in with Microsoft" in client.get("/login").text
    r = client.get("/auth/start?next=/accounts/30", follow_redirects=False)
    assert r.headers["location"].startswith("https://login.microsoftonline.com/")
    r = client.get("/auth/callback?state=s&code=c", follow_redirects=False)
    if allowed:
        assert r.headers["location"] == "/accounts/30"
        assert client.get("/accounts/30").status_code == 200
        assert client.get("/imports").status_code == 403                        # viewer, not admin
    else:
        assert "error=not_allowed" in r.headers["location"]
        assert client.get("/", follow_redirects=False).status_code == 303
