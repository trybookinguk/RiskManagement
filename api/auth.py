"""Dashboard and API sign-in: Microsoft (Entra ID) only, trybooking.co.uk, MFA required.

Every request must come from someone in the TryBooking tenant who:
  * signed in with Microsoft (single-tenant app registration; the token's
    ``tid`` must be AZURE_TENANT_ID and the account must be @trybooking.co.uk,
    WEB_ALLOWED_DOMAINS),
  * holds the app role Risk.Viewer or Risk.Admin (assigned in Entra, ideally
    to a group; "Assignment required" stops anyone else getting a token), and
  * completed MFA. Microsoft enforces it with a Conditional Access policy on
    the app; the app also checks the token says so (WEB_MFA_CHECK):
        amr_or_acrs  (default) ``amr`` contains "mfa", or ``acrs`` contains the
                     Conditional Access authentication context WEB_AUTH_CONTEXT
                     (default c1), which sign-in asks for
        amr | acrs   only that claim
        off          rely on Conditional Access alone (use only if neither
                     claim reaches the tokens; the sign-in error says so)

Browser: OpenID Connect auth-code flow (msal, PKCE + state + nonce), then a
signed session cookie (HMAC-SHA256, 8 hours). Removing someone's role in Entra
takes effect at their next sign-in, within 8 hours.

API: ``Authorization: Bearer <access token>`` issued by Entra for this app's
API (scope Risk.Read). Signature checked against Microsoft's published keys;
issuer, tenant, audience, expiry, scope, role and MFA checked on every call.
App-only tokens (no signed-in user) are refused: there are no unattended jobs.

The audit log is a file, not a table: the warehouse is opened read-only here
and the nightly loads hold long write locks. One JSON line per request: time,
who, method, path, a hash of the query string (search terms are never
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
SESSION_SECONDS = 8 * 3600
ROLES = {"Risk.Admin": "admin", "Risk.Viewer": "viewer"}
API_SCOPE = "Risk.Read"


class AuthError(Exception):
    """Sign-in refused. The message is safe to show the user."""


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


# ------------------------------------------------------------------ claims ---

def _as_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def check_claims(claims: dict, settings) -> dict:
    """{'who', 'role', 'mfa'} for a signed-in TryBooking user, or raises AuthError."""
    tenant = (settings.azure_tenant_id or "").lower()
    if not tenant or str(claims.get("tid", "")).lower() != tenant:
        raise AuthError("This account isn't in the TryBooking Microsoft tenant.")
    who = (claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name")
           or claims.get("email") or "").strip().lower()
    domains = [d.strip().lower().lstrip("@") for d in settings.web_allowed_domains.split(",") if d.strip()]
    if not who or "@" not in who or who.rsplit("@", 1)[1] not in domains:
        raise AuthError(f"Only {', '.join('@' + d for d in domains)} accounts can sign in.")
    roles = [ROLES[r] for r in _as_list(claims.get("roles")) if r in ROLES]
    if not roles:
        raise AuthError("Your account hasn't been given access (app role Risk.Viewer or Risk.Admin). "
                        "Ask the Microsoft 365 admin.")
    mfa = mfa_evidence(claims, settings)
    if mfa is None:
        raise AuthError("Multi-factor authentication is required, and this sign-in didn't use it. "
                        "Sign out of Microsoft and sign in again with MFA.")
    return {"who": who, "role": "admin" if "admin" in roles else "viewer", "mfa": mfa}


def mfa_evidence(claims: dict, settings):
    """How the token shows MFA ('amr', 'acrs:<ctx>', 'conditional-access'), or None."""
    mode = (settings.web_mfa_check or "amr_or_acrs").lower()
    if mode == "off":
        return "conditional-access"
    if mode in ("amr", "amr_or_acrs") and "mfa" in [str(x).lower() for x in _as_list(claims.get("amr"))]:
        return "amr"
    ctx = settings.web_auth_context
    if mode in ("acrs", "amr_or_acrs") and ctx and ctx in _as_list(claims.get("acrs")):
        return f"acrs:{ctx}"
    return None


def claims_request(settings):
    """The OIDC 'claims' parameter asking for the MFA authentication context, or None."""
    ctx = settings.web_auth_context
    if not ctx or (settings.web_mfa_check or "").lower() in ("off", "amr"):
        return None
    want = {"acrs": {"essential": True, "value": ctx}}
    return json.dumps({"id_token": want, "access_token": want})


def entra_configured(settings) -> bool:
    return all([settings.web_entra_client_id, settings.web_entra_client_secret, settings.web_base_url,
                settings.azure_tenant_id])


# ----------------------------------------------------------------- browser ---

class Entra:
    """msal auth-code flow for the dashboard (swapped for a fake in tests)."""

    def __init__(self, settings):
        import msal
        self.settings = settings
        self.redirect_uri = settings.web_base_url.rstrip("/") + "/auth/callback"
        self.app = msal.ConfidentialClientApplication(
            settings.web_entra_client_id, client_credential=settings.web_entra_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}")

    def start(self) -> dict:
        domain = (self.settings.web_allowed_domains.split(",")[0] or "").strip() or None
        return self.app.initiate_auth_code_flow(scopes=[], redirect_uri=self.redirect_uri, domain_hint=domain,
                                                claims_challenge=claims_request(self.settings))

    def finish(self, flow: dict, params: dict) -> dict:
        """ID token claims of the signed-in user, or raises AuthError."""
        try:
            result = self.app.acquire_token_by_auth_code_flow(flow, params)
        except ValueError:                      # state mismatch, replayed or stale callback
            raise AuthError("The sign-in didn't match the one started here. Try again.") from None
        if "error" in result:
            raise AuthError("Microsoft refused the sign-in: " + str(result.get("error_description")
                                                                     or result["error"]).splitlines()[0][:200])
        return result.get("id_token_claims") or {}


# --------------------------------------------------------------------- API ---

class ApiTokens:
    """Validates Entra access tokens sent to /api/... as Authorization: Bearer."""

    def __init__(self, settings, jwks=None):
        import jwt
        self.jwt = jwt
        self.settings = settings
        tenant = settings.azure_tenant_id
        self.jwks = jwks or jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys",
                                            cache_keys=True, lifespan=3600)
        cid = settings.web_entra_client_id
        self.audiences = [cid, f"api://{cid}"] + ([settings.web_api_audience] if settings.web_api_audience else [])
        self.issuers = {f"https://login.microsoftonline.com/{tenant}/v2.0", f"https://sts.windows.net/{tenant}/"}

    def check(self, token: str) -> dict:
        jwt = self.jwt
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences,
                                options={"require": ["exp", "iat", "iss", "aud", "tid"]}, leeway=60)
        except jwt.ExpiredSignatureError:
            raise AuthError("token expired") from None
        except Exception:
            raise AuthError("invalid token") from None
        if claims.get("iss") not in self.issuers:
            raise AuthError("token from the wrong issuer")
        if API_SCOPE not in str(claims.get("scp", "")).split():
            raise AuthError(f"token lacks the {API_SCOPE} scope (app-only tokens aren't accepted)")
        return check_claims(claims, self.settings)


def bearer_challenge(settings) -> str:
    tenant = settings.azure_tenant_id or "common"
    return (f'Bearer authorization_uri="https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize", '
            f'resource_id="api://{settings.web_entra_client_id}"')


# ------------------------------------------------------------------- audit ---

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
