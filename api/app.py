"""RiskManagement dashboard + JSON API (FastAPI).

Run by the riskmanagement-web container:
    uvicorn --factory api.app:create_app --host 0.0.0.0 --port 8085 --proxy-headers

Opens risk.db read-only for every request, so it can never change the
warehouse. Pages are rendered on the server (Jinja2, no JavaScript); every page
has a JSON twin under /api/... returning the same data.

Pages                       JSON
  /                risk       /api/risk?level=account|event&priority=P1&sort=fraud|exposure|risk&dir=desc|asc
  /accounts/{id}              /api/accounts/{id}
  /events/{id}                /api/events/{id}
  /bookings/{ref}             /api/bookings/{ref}      (GUID, BookingTransactionId or BookingId)
  /search?q=                  /api/search?q=
  /imports         (admin)    /api/imports
  /fund-requests              POST /api/fund-requests  {"text": "<table copied from the portal>"}
                              /api/health              (no sign-in)

Sign-in is Microsoft only (api/auth.py): TryBooking tenant, @trybooking.co.uk,
app role Risk.Viewer or Risk.Admin, MFA. Browsers get a session cookie; the
JSON API takes an Entra access token for api://<client id>/Risk.Read.
"""
import json
import os
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from rm import config, db, fund_requests
from . import auth, charts, queries

HERE = os.path.dirname(os.path.abspath(__file__))


class NotSignedIn(Exception):
    """No (valid) session or bearer token. The message is safe to return."""


class Forbidden(Exception):
    pass


def _money(v):
    return "-" if v is None else f"£{v:,.0f}"


def _money2(v):
    return "-" if v is None else f"£{v:,.2f}"


def _num(v, digits=0):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def _date(v, n=10):
    return (str(v) or "")[:n] if v else "-"


PORTAL_ACCOUNT = ("https://portal.trybooking.com/uk/admin/impersonate;redirectTo=%2Fdashboard;"
                  "accountId={aid};userId=undefined")
MAX_PASTE = 1_000_000     # bytes of pasted fund requests


def portal_url(aid):
    """The account in the TryBooking admin portal (impersonate, then its dashboard)."""
    return PORTAL_ACCOUNT.format(aid=int(aid))


def _inline_css() -> str:
    """app.css with the Figtree font embedded, put into every page's <style>.

    Embedded rather than linked so the look never depends on a second request:
    ngrok's free plan answers browser requests for /static/... with its warning
    page, and the browser then drops the stylesheet (seen on 6 Nov 2026)."""
    import base64
    static = os.path.join(HERE, "static")
    with open(os.path.join(static, "app.css"), encoding="utf-8") as f:
        css = f.read()
    with open(os.path.join(static, "figtree-latin.woff2"), "rb") as f:
        font = base64.b64encode(f.read()).decode()
    return css.replace("url(/static/figtree-latin.woff2)", f"url(data:font/woff2;base64,{font})")


def create_app(settings=None, entra=None, today=None, api_tokens=None) -> FastAPI:
    settings = settings or config.get_settings()
    signer = auth.Signer()
    audit = auth.Audit(os.path.join(settings.data_dir, "logs"))
    problems = auth.config_problems(settings)
    if auth.entra_configured(settings) and not problems:
        entra = entra or auth.Entra(settings)
        api_tokens = api_tokens or auth.ApiTokens(settings)
    state = {"hasher": None}

    app = FastAPI(title="RiskManagement", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
    templates.env.filters.update(money=_money, money2=_money2, num=_num, d=_date)
    templates.env.globals.update(chart=lambda *a, **k: Markup(charts.line(*a, **k)), entra=entra is not None,
                                 config_problems=problems, page_css=Markup(_inline_css()),
                                 portal_url=portal_url)

    def connect():
        if not os.path.exists(settings.risk_db):
            raise HTTPException(503, "The warehouse hasn't been created yet (rmctl.sh setup).")
        return db.connect(settings.risk_db, readonly=True)

    def hasher(conn):
        if state["hasher"] is None and settings.risk_passphrase:
            try:
                state["hasher"] = db.get_hasher(conn, settings.risk_passphrase)
            except Exception:
                return None
        return state["hasher"]

    state_file = os.path.join(settings.data_dir, "web", "state.json")

    def web_state():
        try:
            with open(state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_web_state(**changes):
        data = dict(web_state(), **changes)
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        tmp = state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, state_file)

    fixed_today = today

    def today():
        if fixed_today:
            return fixed_today
        return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()

    # ------------------------------------------------------------ identity --
    def user(request: Request, conn):
        """Signed-in user from an Entra bearer token (API) or the session cookie (browser)."""
        cached = getattr(request.state, "user", None)
        if cached:
            return cached
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer "):
            if api_tokens is None:
                raise NotSignedIn("Microsoft sign-in isn't set up on this server")
            try:
                u = api_tokens.check(authz[7:].strip())
            except auth.AuthError as e:
                raise NotSignedIn(str(e)) from None
            request.state.user = dict(u, kind="api")
            return request.state.user
        data = signer.loads(request.cookies.get(auth.SESSION_COOKIE, ""))
        if data:
            request.state.user = data
            return data
        return None

    def require(request: Request, conn, role="viewer"):
        u = user(request, conn)
        if not u:
            raise NotSignedIn()
        if role == "admin" and u.get("role") != "admin":
            raise Forbidden()
        return u

    def page(request, name, conn, **ctx):
        return templates.TemplateResponse(request, name, {"user": getattr(request.state, "user", None), **ctx})

    # ---------------------------------------------------------- middleware --
    # The dashboard is reachable from the internet (router forwards 8443 to the DSM
    # reverse proxy), so: only answer for its own host name (plus localhost for the
    # container health check), mark cookies Secure whenever the site is HTTPS, and
    # say nothing about the warehouse before sign-in.
    base = urllib.parse.urlsplit(settings.web_base_url) if settings.web_base_url else None
    https_site = bool(base and base.scheme == "https")
    allowed_hosts = {"localhost", "127.0.0.1"} | ({base.hostname.lower()} if base and base.hostname else set())

    @app.middleware("http")
    async def secure(request: Request, call_next):
        t0 = time.monotonic()
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if base and host not in allowed_hosts:
            return JSONResponse({"error": "unknown host"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
                                                       "img-src 'self' "
                                                       "data:; form-action 'self' https://login.microsoftonline.com; "
                                                       "frame-ancestors 'none'")
        static = request.url.path.startswith("/static/")
        response.headers["Cache-Control"] = "public, max-age=86400" if static else "no-store"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if https_site:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        u = getattr(request.state, "user", None)
        if not static and request.url.path not in ("/api/health", "/favicon.ico", "/login", "/signed-out"):
            audit.write(u["who"] if u else None, request.method, request.url.path, request.url.query,
                        response.status_code, int((time.monotonic() - t0) * 1000))
        return response

    @app.exception_handler(NotSignedIn)
    async def not_signed_in(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc) or "send Authorization: Bearer <Entra access token for "
                                                       f"api://{settings.web_entra_client_id}/{auth.API_SCOPE}>"},
                                status_code=401, headers={"WWW-Authenticate": auth.bearer_challenge(settings)})
        nxt = urllib.parse.quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "admin only"}, status_code=403)
        return HTMLResponse("<h1>Admins only</h1><p><a href='/'>Back</a></p>", status_code=403)

    def same_origin(request):
        """Refuse a form posted from another site (the session cookie is SameSite=Lax too)."""
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return
        o = urllib.parse.urlsplit(origin)
        host = (request.headers.get("host") or "").lower()
        if o.netloc.lower() != host:
            raise HTTPException(403, "This form must be sent from the dashboard itself.")

    def _safe_next(nxt):
        return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"

    def _set_session(resp, request, data):
        resp.set_cookie(auth.SESSION_COOKIE, signer.dumps(data, auth.SESSION_SECONDS), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="lax", secure=https_site or request.url.scheme == "https")

    # ------------------------------------------------------------- sign-in --
    MSG_COOKIE = "rm_msg"

    def _to_login(request, message):
        resp = RedirectResponse("/login", status_code=303)
        resp.set_cookie(MSG_COOKIE, signer.dumps({"m": message}, 120), max_age=120, httponly=True, samesite="lax",
                        secure=https_site or request.url.scheme == "https")
        resp.delete_cookie(auth.FLOW_COOKIE)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        msg = (signer.loads(request.cookies.get(MSG_COOKIE, "")) or {}).get("m")
        resp = templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": msg,
                                                                  "user": None, "domains": settings.web_allowed_domains})
        resp.delete_cookie(MSG_COOKIE)
        return resp

    @app.get("/auth/start")
    def auth_start(request: Request, next: str = "/"):
        if entra is None:
            raise HTTPException(503, "Microsoft sign-in isn't set up yet (WEB_ENTRA_* in .env).")
        flow = entra.start()
        flow["next"] = _safe_next(next)
        resp = RedirectResponse(flow["auth_uri"], status_code=303)
        resp.set_cookie(auth.FLOW_COOKIE, signer.dumps({"flow": flow}, 600), max_age=600, httponly=True,
                        samesite="lax", secure=https_site or request.url.scheme == "https")
        return resp

    @app.get("/auth/callback")
    def auth_callback(request: Request):
        if entra is None:
            raise HTTPException(503, "Microsoft sign-in isn't set up yet (WEB_ENTRA_* in .env).")
        data = signer.loads(request.cookies.get(auth.FLOW_COOKIE, ""))
        if not data:
            return _to_login(request, "The sign-in took too long or was started elsewhere. Try again.")
        flow = data["flow"]
        try:
            u = auth.check_claims(entra.finish(flow, dict(request.query_params)), settings)
        except auth.AuthError as e:
            request.state.user = {"who": "(refused)"}
            return _to_login(request, str(e))
        request.state.user = u
        resp = RedirectResponse(_safe_next(flow.get("next")), status_code=303)
        _set_session(resp, request, {"who": u["who"], "role": u["role"], "mfa": u["mfa"], "kind": "entra"})
        resp.delete_cookie(auth.FLOW_COOKIE)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/signed-out", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE)
        return resp

    @app.get("/signed-out", response_class=HTMLResponse)
    def signed_out(request: Request):
        return templates.TemplateResponse(request, "login.html", {"next": "/", "error": None, "user": None,
                                                                  "signed_out": True,
                                                                  "domains": settings.web_allowed_domains})

    # --------------------------------------------------------------- pages --
    def _risk(conn, level, priority, limit, offset, sort="fraud", dir="desc"):
        if priority and priority not in queries.PRIORITIES:
            raise HTTPException(400, "priority must be P1-P4 or -")
        if sort not in queries.SORTS:
            raise HTTPException(400, "sort must be fraud, exposure or risk")
        limit = max(1, min(int(limit), 500))
        data = queries.risk_list(conn, level, None, priority, limit, max(0, int(offset)), sort, dir != "asc")
        r = queries.runs(conn, 2)
        data["counts"] = queries.priority_counts(conn, data["run"], level)
        data["at_risk"] = queries.priority_at_risk(conn, data["run"], level)
        data["previous"] = r[1] if len(r) > 1 else None
        data["previous_counts"] = queries.priority_counts(conn, data["previous"], level)
        return data

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, level: str = "account", priority: str = "", limit: int = 50, offset: int = 0,
             sort: str = "fraud", dir: str = "desc"):
        conn = connect()
        try:
            require(request, conn)
            level = "event" if level == "event" else "account"
            data = _risk(conn, level, priority or None, limit, offset, sort, dir)
            return page(request, "home.html", conn, level=level, priority=priority, limit=limit, offset=offset,
                        priorities=queries.PRIORITIES, **data)
        finally:
            conn.close()

    @app.get("/accounts/{aid}", response_class=HTMLResponse)
    def account_page(request: Request, aid: int):
        conn = connect()
        try:
            require(request, conn)
            data = queries.account(conn, aid, today())
            if not data:
                raise HTTPException(404, f"No account {aid}")
            return page(request, "account.html", conn, a=data,
                        balance_points=[(b["SnapshotDate"], b["FullBalance"]) for b in data["balances"]],
                        score_points=[(r["RunDate"], r["FraudScore"]) for r in data["scores"]])
        finally:
            conn.close()

    @app.get("/events/{eid}", response_class=HTMLResponse)
    def event_page(request: Request, eid: int):
        conn = connect()
        try:
            require(request, conn)
            data = queries.event(conn, eid)
            if not data:
                raise HTTPException(404, f"No event {eid}")
            return page(request, "event.html", conn, e=data,
                        score_points=[(r["RunDate"], r["FraudScore"]) for r in data["scores"]])
        finally:
            conn.close()

    @app.get("/bookings/{ref}", response_class=HTMLResponse)
    def booking_page(request: Request, ref: str):
        conn = connect()
        try:
            require(request, conn)
            data = queries.booking(conn, ref)
            if not data:
                raise HTTPException(404, "No booking with that reference")
            return page(request, "booking.html", conn, b=data, ref=ref)
        finally:
            conn.close()

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        conn = connect()
        try:
            require(request, conn)
            data = queries.search(conn, q, hasher(conn))
            if len(data["results"]) == 1 and data["kind"] in ("number", "guid") and data["results"][0]["url"]:
                return RedirectResponse(data["results"][0]["url"], status_code=303)
            return page(request, "search.html", conn, q=q, **data)
        finally:
            conn.close()

    @app.get("/imports", response_class=HTMLResponse)
    def imports_page(request: Request):
        conn = connect()
        try:
            require(request, conn, "admin")
            st = web_state()
            return page(request, "imports.html", conn, i=queries.imports(conn, failures_cleared=st.get(
                "failures_cleared", 0)), cleared=st.get("failures_cleared_by"))
        finally:
            conn.close()

    @app.post("/imports/clear-failures")
    def clear_failures(request: Request):
        """Hide the failures listed now (admin). Kept in data/web/state.json: the warehouse is
        never written by the dashboard. New failures show up again."""
        same_origin(request)
        conn = connect()
        try:
            u = require(request, conn, "admin")
            last = conn.execute("SELECT MAX(id) FROM import_log WHERE status='failed'").fetchone()[0] or 0
        finally:
            conn.close()
        save_web_state(failures_cleared=last, failures_cleared_by={
            "who": u["who"], "at": datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")})
        return RedirectResponse("/imports", status_code=303)

    # ------------------------------------------------------ fund requests --
    async def _pasted(request):
        body = await request.body()
        if len(body) > MAX_PASTE:
            raise HTTPException(413, "That's too much to paste at once (1 MB). Paste fewer rows.")
        if request.url.path.startswith("/api/"):
            try:
                return str(json.loads(body or b"{}").get("text") or "")
            except (ValueError, AttributeError):
                raise HTTPException(400, 'send JSON {"text": "<table copied from the portal>"}') from None
        return (urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("text") or [""])[0]

    @app.get("/fund-requests", response_class=HTMLResponse)
    def fund_requests_page(request: Request):
        conn = connect()
        try:
            require(request, conn)
            return page(request, "fund_requests.html", conn, result=None, cfg=fund_requests.CONFIG)
        finally:
            conn.close()

    @app.post("/fund-requests", response_class=HTMLResponse)
    async def fund_requests_check(request: Request):
        """Risk check of fund transfer requests pasted from the portal. Nothing pasted is kept:
        the audit log records the request, not its contents."""
        same_origin(request)
        conn = connect()
        try:
            require(request, conn)
            text = await _pasted(request)
            result = fund_requests.assess(conn, text, today())
            return page(request, "fund_requests.html", conn, result=result, cfg=fund_requests.CONFIG)
        finally:
            conn.close()

    # ---------------------------------------------------------------- JSON --
    def api(request, fn, role="viewer"):
        conn = connect()
        try:
            require(request, conn, role)
            return fn(conn)
        finally:
            conn.close()

    @app.get("/api/health")
    def api_health():
        """Public liveness check: says nothing about the warehouse (details: /api/status, signed in)."""
        ok = os.path.exists(settings.risk_db)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)

    @app.get("/api/status")
    def api_status(request: Request):
        return api(request, lambda c: {"ok": True, "sign_in": "microsoft" if entra is not None else "not configured",
                                       **queries.health(c)})

    @app.get("/api/risk")
    def api_risk(request: Request, level: str = "account", priority: str = "", limit: int = 50, offset: int = 0,
                 sort: str = "fraud", dir: str = "desc"):
        level = "event" if level == "event" else "account"
        return api(request, lambda c: _risk(c, level, priority or None, limit, offset, sort, dir))

    def _found(x, what):
        if not x:
            raise HTTPException(404, f"no such {what}")
        return x

    @app.get("/api/accounts/{aid}")
    def api_account(request: Request, aid: int):
        return api(request, lambda c: _found(queries.account(c, aid, today()), "account"))

    @app.get("/api/events/{eid}")
    def api_event(request: Request, eid: int):
        return api(request, lambda c: _found(queries.event(c, eid), "event"))

    @app.get("/api/bookings/{ref}")
    def api_booking(request: Request, ref: str):
        return api(request, lambda c: _found(queries.booking(c, ref), "booking"))

    @app.get("/api/search")
    def api_search(request: Request, q: str = ""):
        return api(request, lambda c: queries.search(c, q, hasher(c)))

    @app.get("/api/me")
    def api_me(request: Request):
        """Who you're signed in as, your role, and how the token showed MFA (amr, acrs:c1, ...)."""
        def me(conn):
            u = require(request, conn)
            return {"who": u["who"], "role": u["role"], "mfa": u.get("mfa"),
                    "via": "access token" if u.get("kind") == "api" else "browser session"}
        return api(request, me)

    @app.get("/api/imports")
    def api_imports(request: Request):
        return api(request, lambda c: queries.imports(c, failures_cleared=web_state().get("failures_cleared", 0)),
                   "admin")

    @app.post("/api/fund-requests")
    async def api_fund_requests(request: Request):
        text = await _pasted(request)
        return api(request, lambda c: fund_requests.assess(c, text, today()))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail,
                                                                  "user": getattr(request.state, "user", None)},
                                          status_code=exc.status_code)

    return app

