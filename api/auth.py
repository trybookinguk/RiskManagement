"""Dashboard sign-in, sessions, API tokens and the audit log.

Two ways in:
  * API token (always available): ``rmctl.sh run rm_admin.py create-token NAME``.
    Scripts send ``Authorization: Bearer <token>``; people paste it once on the
    sign-in page and get a session cookie.
  * Sign in with Microsoft (Entra ID), when WEB_ENTRA_CLIENT_ID,
    WEB_ENTRA_CLIENT_SECRET, WEB_BASE_URL and AZURE_TENANT_ID are set. Only
    active addresses in dashboard_users (``rmctl.sh add-user``) get in.

Sessions are a signed cookie (HMAC-SHA256, 12 hours). The signing key is made
fresh each time the web container starts, so a restart signs everyone out.

The audit log is a file, not a table: the warehouse is opened read-only here
and the nightly loads hold long write locks. One JSON line per request:
time, who, method, path, a hash of the query string (search terms are never
written), status, duration.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone

SESSION_COOKIE = "rm_session"
FLOW_COOKIE = "rm_flow"
SESSION_SECONDS = 12 * 3600


class Signer:
    def __init__(self, key: bytes = None):
        self.key = key or secrets.token_bytes(32)

    def dumps(self, data: dict, seconds: int) -> str:
        body = dict(data, exp=int(time.time()) + seconds)
        raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
        return f"{raw}.{sig}"

    def loads(self, value: str):
        try:
            raw, sig = value.rsplit(".", 1)
        except (AttributeError, ValueError):
            return None
        good = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, TypeError):
            return None
        if data.get("exp", 0) < time.time():
            return None
        return data


def check_token(conn, token: str):
    """Token name if ``token`` is a valid, unrevoked API token, else None."""
    token = (token or "").strip()
    if not token.startswith("tok_") or "." not in token:
        return None
    token_id = token.split(".", 1)[0]
    row = conn.execute("SELECT Name, TokenHash, Revoked FROM api_tokens WHERE TokenId=?", (token_id,)).fetchone()
    if not row or row["Revoked"]:
        return None
    if not hmac.compare_digest(row["TokenHash"], hashlib.sha256(token.encode()).hexdigest()):
        return None
    return row["Name"]


def dashboard_role(conn, email: str):
    row = conn.execute("SELECT Role FROM dashboard_users WHERE Email=? AND Active=1",
                       ((email or "").strip().lower(),)).fetchone()
    return row["Role"] if row else None


class Entra:
    """Thin wrapper over msal's auth-code flow (swapped for a fake in tests)."""

    def __init__(self, settings):
        import msal
        self.redirect_uri = settings.web_base_url.rstrip("/") + "/auth/callback"
        self.app = msal.ConfidentialClientApplication(
            settings.web_entra_client_id, client_credential=settings.web_entra_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}")

    def start(self) -> dict:
        return self.app.initiate_auth_code_flow(scopes=[], redirect_uri=self.redirect_uri)

    def finish(self, flow: dict, params: dict) -> str:
        """Signed-in email address, or raises ValueError."""
        result = self.app.acquire_token_by_auth_code_flow(flow, params)
        if "error" in result:
            raise ValueError(result.get("error_description") or result["error"])
        claims = result.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
        if not email:
            raise ValueError("Microsoft did not return an email address")
        return email.lower()


def entra_configured(settings) -> bool:
    return all([settings.web_entra_client_id, settings.web_entra_client_secret, settings.web_base_url,
                settings.azure_tenant_id])


class Audit:
    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def write(self, who, method, path, query, status, ms):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            now = datetime.now(timezone.utc)
            line = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "who": who, "method": method, "path": path,
                    "query": hashlib.sha256(query.encode()).hexdigest()[:16] if query else None,
                    "status": status, "ms": ms}
            with open(os.path.join(self.log_dir, f"web-audit-{now:%Y%m}.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass  # auditing must never take the dashboard down
