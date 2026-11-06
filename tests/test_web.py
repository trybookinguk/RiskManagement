"""Dashboard + JSON API (FastAPI TestClient) on the fireworks scenario warehouse.

Sign-in is Microsoft only. The browser flow uses a fake Entra that returns ID
token claims; the API is tested with real RS256 JWTs signed by a test key whose
public half stands in for Microsoft's published keys.
"""
import html
import importlib.util
import json
import pathlib
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rm import config, db  # noqa: E402
from api import auth  # noqa: E402
from test_risk_signals import TODAY, scenario  # noqa: E402,F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def id_claims(**over):
    c = {"tid": TENANT, "preferred_username": "Risk.Analyst@trybooking.co.uk", "roles": ["Risk.Viewer"],
         "amr": ["pwd", "mfa"], "aud": CLIENT}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


class FakeEntra:
    def __init__(self):
        self.claims = id_claims()

    def start(self):
        return {"auth_uri": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize?state=s", "state": "s"}

    def finish(self, flow, params):
        if params.get("state") != flow["state"]:
            raise auth.AuthError("The sign-in didn't match the one started here. Try again.")
        return self.claims


class FakeJWKS:
    class _K:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._K()


def access_token(key=KEY, **over):
    now = int(time.time())
    c = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "tid": TENANT,
         "iat": now, "nbf": now, "exp": now + 3600, "scp": "Risk.Read",
         "preferred_username": "risk.analyst@trybooking.co.uk", "roles": ["Risk.Viewer"], "amr": ["pwd", "mfa"]}
    c.update(over)
    return jwt.encode({k: v for k, v in c.items() if v is not None}, key, algorithm="RS256")


@pytest.fixture
def entra_env(monkeypatch):
    for k, v in {"AZURE_TENANT_ID": TENANT, "WEB_ENTRA_CLIENT_ID": CLIENT, "WEB_ENTRA_CLIENT_SECRET": "x",
                 "WEB_BASE_URL": "https://risk.trybooking.co.uk"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def site(scenario, entra_env):
    assert _load("risk_report", "bin/risk_report.py").main(["--no-email", "--today", TODAY]) == 0
    return make_client()


def make_client():
    from api.app import create_app
    s = config.get_settings()
    entra = FakeEntra()
    client = TestClient(create_app(s, entra=entra, today=TODAY, api_tokens=auth.ApiTokens(s, jwks=FakeJWKS())),
                        base_url="https://risk.trybooking.co.uk")
    client.entra = entra
    return client


def sign_in(client, next_="/", **claims):
    client.entra.claims = id_claims(**claims)
    r = client.get(f"/auth/start?next={next_}", follow_redirects=False)
    assert r.headers["location"].startswith("https://login.microsoftonline.com/")
    return client.get("/auth/callback?state=s&code=c", follow_redirects=False)


@pytest.fixture
def viewer(site):
    r = sign_in(site)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return site


# ------------------------------------------------------------- sign-in ---

def test_sign_in_required(site):
    r = site.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = site.get("/api/risk")
    assert r.status_code == 401 and "login.microsoftonline.com" in r.headers["WWW-Authenticate"]
    assert site.get("/api/health").json() == {"ok": True}                       # public: nothing more
    assert site.get("/api/status").status_code == 401
    login = site.get("/login").text
    assert "Sign in with Microsoft" in login and "tok_" not in login and 'name="token"' not in login
    assert site.post("/login", data={"token": "tok_x.y"}).status_code == 405      # token sign-in is gone


def test_session_cookie_is_secure(site):
    r = sign_in(site)
    cookie = r.headers["set-cookie"]
    assert "rm_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()


@pytest.mark.parametrize("claims, message", [
    ({"tid": "99999999-0000-0000-0000-000000000000"}, "isn't in the TryBooking Microsoft tenant"),
    ({"preferred_username": "someone@gmail.com"}, "Only @trybooking.co.uk accounts"),
    ({"roles": None}, "hasn't been given access"),
    ({"roles": ["SomethingElse"]}, "hasn't been given access"),
    ({"amr": ["pwd"]}, "Multi-factor authentication is required"),
    ({"amr": None}, "Multi-factor authentication is required"),
])
def test_sign_in_refused(site, claims, message):
    r = sign_in(site, **claims)
    assert r.headers["location"] == "/login" and "rm_session=" not in r.headers.get("set-cookie", "")
    assert message in html.unescape(site.get("/login").text)
    assert site.get("/", follow_redirects=False).status_code == 303


def test_mfa_by_authentication_context(site, monkeypatch):
    r = sign_in(site, amr=["pwd"], acrs=["c1"])                     # Conditional Access auth context satisfied
    assert r.headers["location"] == "/"
    req = json.loads(auth.claims_request(config.get_settings()))
    assert req["id_token"]["acrs"] == {"essential": True, "value": "c1"}


def test_mfa_check_off_relies_on_conditional_access(scenario, entra_env, monkeypatch):
    monkeypatch.setenv("WEB_MFA_CHECK", "off")
    client = make_client()
    assert sign_in(client, amr=["pwd"]).headers["location"] == "/"
    assert auth.claims_request(config.get_settings()) is None


def test_roles_and_open_redirect(site):
    assert sign_in(site, next_="//evil.example").headers["location"] == "/"
    assert site.get("/imports").status_code == 403                                  # viewer
    sign_in(site, roles=["Risk.Viewer", "Risk.Admin"])
    assert site.get("/imports").status_code == 200
    assert "· admin" in site.get("/").text
    me = site.get("/api/me").json()
    assert me == {"who": "risk.analyst@trybooking.co.uk", "role": "admin", "mfa": "amr", "via": "browser session"}
    r = site.post("/logout", follow_redirects=False)
    assert r.headers["location"] == "/signed-out"


def test_not_configured(scenario, monkeypatch):
    from api.app import create_app
    client = TestClient(create_app(config.get_settings(), today=TODAY))
    assert "isn't set up on this server yet" in html.unescape(client.get("/login").text)
    assert client.get("/auth/start").status_code == 503
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/risk", headers={"Authorization": f"Bearer {access_token()}"}).status_code == 401


# ----------------------------------------------------------------- pages ---

def test_risk_pages(viewer):
    page = viewer.get("/").text
    assert "Sparkle Events" in page and "P1" in page and "account created 01:30 (night)" in page
    assert "risk.analyst@trybooking.co.uk" in page
    assert "Fireworks Spectacular" in viewer.get("/?level=event&priority=P1").text
    acc = viewer.get("/accounts/30").text
    for text in ("Sparkle Events", "Account created", "Bank transfers", "no bank transfers yet", "Fireworks Spectacular"):
        assert text in acc, text
    acc40 = viewer.get("/accounts/40").text
    assert "24 (£12,000)" in acc40 and "12 (£60,000)" in acc40
    ev = viewer.get("/events/300").text
    assert "Average ticket" in ev and "£100" in ev and "Payment attempts" in ev
    assert viewer.get("/accounts/999999").status_code == 404


def test_search(viewer):
    r = viewer.get("/search?q=30", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/accounts/30"
    assert "Village Bonfire" in viewer.get("/search?q=bonfire").text
    guid = "00000000-0000-4000-8000-000000003000"
    r = viewer.get(f"/search?q={guid.upper().replace('-', '')}", follow_redirects=False)
    assert r.headers["location"] == f"/bookings/{guid}"
    b = viewer.get(f"/bookings/{guid}").text
    assert "Fireworks Spectacular" in b and "Successful" in b and "jane@example.com" not in b
    found = viewer.get("/api/search", params={"q": "jane@example.com"}).json()     # session works for /api too
    assert found["kind"] == "email" and len(found["results"]) == 20
    assert viewer.get("/api/search", params={"q": "VISA 4242"}).json()["kind"] == "card"
    assert viewer.get("/api/search", params={"q": "07700 900123"}).json()["kind"] == "phone"


def test_audit_log_has_no_search_terms(viewer):
    viewer.get("/search", params={"q": "jane@example.com"})
    text = next((pathlib.Path(config.get_settings().data_dir) / "logs").glob("web-audit-*.log")).read_text()
    assert "jane@example.com" not in text and "jane" not in text
    s = [json.loads(l) for l in text.splitlines() if '"/search"' in l][0]
    assert s["who"] == "risk.analyst@trybooking.co.uk" and len(s["query"]) == 16


def test_security_headers_and_read_only(viewer):
    r = viewer.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "default-src 'none'" in r.headers["Content-Security-Policy"]
    conn = db.connect(config.get_settings().risk_db, readonly=True)
    with pytest.raises(Exception):
        conn.execute("DELETE FROM accounts")
    conn.close()


# ------------------------------------------------------------------- API ---

def _api(client, token, path="/api/risk?level=event&priority=P1"):
    return client.get(path, headers={"Authorization": f"Bearer {token}"})


def test_api_with_entra_access_token(site):
    r = _api(site, access_token())
    assert r.status_code == 200 and [x["Id"] for x in r.json()["rows"]] == [300]
    assert _api(site, access_token(aud=f"api://{CLIENT}")).status_code == 200
    assert _api(site, access_token(iss=f"https://sts.windows.net/{TENANT}/", upn="risk.analyst@trybooking.co.uk",
                                   preferred_username=None)).status_code == 200            # v1.0 token
    acc = _api(site, access_token(), "/api/accounts/40").json()
    assert acc["transfers"]["count"] == 24 and acc["past"]["events"] == 12
    assert _api(site, access_token(), "/api/imports").status_code == 403                   # viewer
    assert _api(site, access_token(), "/api/me").json()["via"] == "access token"
    assert _api(site, access_token(roles=["Risk.Admin"]), "/api/imports").status_code == 200


@pytest.mark.parametrize("over, why", [
    ({"exp": int(time.time()) - 3600}, "token expired"),
    ({"tid": "99999999-0000-0000-0000-000000000000"}, "invalid token"),       # issuer check fails first? see below
    ({"aud": "api://someone-else"}, "invalid token"),
    ({"iss": "https://login.microsoftonline.com/common/v2.0"}, "wrong issuer"),
    ({"scp": None}, "Risk.Read scope"),                                          # app-only token
    ({"amr": ["pwd"]}, "Multi-factor authentication is required"),
    ({"roles": None}, "hasn't been given access"),
    ({"preferred_username": "x@gmail.com"}, "Only @trybooking.co.uk"),
])
def test_api_refuses(site, over, why):
    r = _api(site, access_token(**over))
    assert r.status_code == 401
    if over.get("tid"):
        assert "TryBooking Microsoft tenant" in r.json()["error"]
    else:
        assert why in r.json()["error"]


def test_api_refuses_forged_and_static_tokens(site):
    assert _api(site, access_token(key=OTHER_KEY)).json()["error"] == "invalid token"
    assert _api(site, "tok_abcd.secret").status_code == 401
    assert _api(site, "not-a-jwt").status_code == 401
    hs = jwt.encode({"tid": TENANT, "aud": CLIENT}, "s" * 32, algorithm="HS256")             # algorithm swap
    assert _api(site, hs).status_code == 401


def test_internet_facing_hardening(viewer):
    r = viewer.get("/")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "camera=()" in r.headers["Permissions-Policy"]
    status = viewer.get("/api/status").json()
    assert status["sign_in"] == "microsoft" and status["schema"] >= 7
    # Requests for any other host name (scanners hitting the IP, Host-header tricks) get nothing.
    assert viewer.get("/", headers={"host": "145.224.66.149:8443"}).status_code == 400
    assert viewer.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    assert viewer.get("/api/health", headers={"host": "localhost:8085"}).json() == {"ok": True}   # rmctl web-status


def test_cookie_secure_even_if_proxy_says_http(scenario, entra_env):
    from fastapi.testclient import TestClient as TC
    from api.app import create_app
    s = config.get_settings()
    client = TC(create_app(s, entra=FakeEntra(), today=TODAY, api_tokens=auth.ApiTokens(s, jwks=FakeJWKS())),
                base_url="http://risk.trybooking.co.uk")
    client.entra = FakeEntra()
    r = sign_in(client)
    assert "Secure" in r.headers["set-cookie"]
