"""The HTTP API: authentication, then a thin skin over ControlPlane.

Nothing in here decides anything. Every rule (ownership, quotas, capabilities,
sealing) lives in ControlPlane and sirosid_core.policy; a handler authenticates the
caller, parses JSON, calls one method and maps its exceptions to a status code. A
rule added here would be a rule MCP and the CLI do not have.

What this layer does own is the web's own risks:
  * the session cookie is `__Host-` prefixed (HTTPS only, no Domain, path /), HttpOnly
    and SameSite=Strict;
  * every state-changing request must carry an Origin header on the allow-list (a
    cookie alone is never enough: that is the CSRF defence), and bodies must be JSON;
  * bodies are size-capped;
  * the unauthenticated endpoints that take guesses (invite tokens, ceremonies) are
    rate limited per client;
  * every response carries restrictive security headers and is never cached.
Bytes in JSON (the key container, the main key) travel as base64url.
"""
import base64
import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from sirosid_core.policy import PolicyError

from .auth import AuthError, AuthService
from .service import (ControlPlane, Forbidden, InvalidInvite, InvalidState, NotFound, NotSignedIn, QuotaExceeded,
                      ServiceError)
from .vault import Locked, VaultError

log = logging.getLogger("sirosid.api")

COOKIE = "__Host-sid"
MAX_BODY = 256 * 1024
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": "publickey-credentials-get=(self), publickey-credentials-create=(self)",
}

# What the console's own pages may do: run its own scripts, load its own stylesheet, talk to
# its own origin. No inline script or style, no framing, no other origin, no forms.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
STATIC_DIRS = ("js", "css")


@dataclass
class ApiConfig:
    origins: Tuple[str, ...] = ("https://console.sirosid.dev",)
    cookie_max_age: float = 12 * 3600.0
    # Header carrying the real client address behind a trusted proxy (Fly sets
    # Fly-Client-IP). Empty = use the socket peer. Never trust it unless a proxy you
    # control sets it, or a client can pick its own rate-limit bucket.
    client_ip_header: str = ""
    # Directory with index.html, js/ and css/ (the console). None = API only.
    console_dir: Optional[str] = None
    # The public https origin applications are told about (OAuth issuer, MCP resource). Defaults to origins[0].
    public_url: str = ""


class RateLimited(ServiceError):
    pass


class RateLimiter:
    """Sliding window, in memory, per (bucket, client). Single process: fine for v1."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._hits: Dict[Tuple[str, str], List[float]] = {}

    def check(self, bucket: str, client: str, limit: int, window: float):
        now = self.clock()
        key = (bucket, client)
        hits = [t for t in self._hits.get(key, []) if t > now - window]
        if len(hits) >= limit:
            self._hits[key] = hits
            raise RateLimited("too many attempts; wait a moment")
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 20000:                       # bound memory under a spray
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - window}


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s) -> bytes:
    if not isinstance(s, str):
        raise ServiceError("expected a base64url string")
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise ServiceError("not valid base64url") from None


@dataclass
class Result:
    payload: object = None
    status: int = 200
    set_session: Optional[str] = None
    clear_session: bool = False


def status_for(e: Exception) -> Tuple[int, dict]:
    if isinstance(e, PolicyError):
        return 422, {"error": "invalid_config", "problems": [{"path": p.path, "message": p.message} for p in e.problems]}
    for cls, code, name in ((NotSignedIn, 401, "not_signed_in"), (Locked, 423, "locked"), (RateLimited, 429, "rate_limited"),
                            (AuthError, 401, "auth_failed"), (InvalidInvite, 400, "invalid_invite"), (NotFound, 404, "not_found"),
                            (Forbidden, 403, "forbidden"), (QuotaExceeded, 409, "quota"), (InvalidState, 409, "invalid_state"),
                            (ServiceError, 400, "bad_request")):
        if isinstance(e, cls):
            return code, {"error": name, "message": str(e)}
    return 500, {"error": "internal", "message": "something went wrong"}


class Api:
    def __init__(self, cp: ControlPlane, auth: AuthService, config: ApiConfig = None, clock=None):
        self.cp, self.auth = cp, auth
        self.config = config or ApiConfig(origins=auth.config.origins)
        self.limiter = RateLimiter(clock or cp.clock)
        from .oauth import OAuthService
        self.oauth = OAuthService(cp, self.config.public_url or self.config.origins[0])

    # ---- plumbing ----------------------------------------------------------------------------

    def _client(self, request: Request) -> str:
        h = self.config.client_ip_header
        if h and request.headers.get(h):
            return request.headers[h].split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _origin_ok(self, request: Request):
        if request.method not in UNSAFE:
            return
        origin = request.headers.get("origin")
        if origin not in self.config.origins:
            raise Forbidden("this request did not come from the console")

    async def _body(self, request: Request) -> dict:
        if request.method not in UNSAFE:
            return {}
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ServiceError("request too large")
        if not raw:
            return {}
        if "application/json" not in request.headers.get("content-type", ""):
            raise ServiceError("send application/json")
        try:
            data = json.loads(raw)
        except ValueError:
            raise ServiceError("the body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ServiceError("the body must be a JSON object")
        return data

    def _principal(self, request: Request):
        return self.auth.principal_from_token(request.cookies.get(COOKIE))

    def route(self, path: str, methods: List[str], fn: Callable, *, public=False, admin=False, rate=None):
        """fn(who, data, params, request) -> Result | dict. Runs in a thread."""
        async def endpoint(request: Request) -> Response:
            try:
                self._origin_ok(request)
                if rate:
                    self.limiter.check(path, self._client(request), *rate)
                data = await self._body(request)
                who = None if public else self._principal(request)
                if admin and not who.is_admin:
                    raise NotFound("not found")                # admin routes do not advertise themselves
                out = await run_in_threadpool(fn, who, data, request.path_params, request)
            except Exception as e:                              # noqa: BLE001 - mapped below
                code, payload = status_for(e)
                if code == 500:
                    log.exception("unhandled error in %s", path)
                elif code in (401, 403, 423):
                    log.info("%s -> %s %s", path, code, payload.get("error"))
                return self._finish(Result(payload, code))
            return self._finish(out if isinstance(out, Result) else Result(out))
        return Route(path, endpoint, methods=methods)

    def _finish_empty(self, status: int) -> Response:
        resp = Response(status_code=status)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        return resp

    def _finish(self, r: Result) -> Response:
        resp = JSONResponse(r.payload if r.payload is not None else {}, status_code=r.status)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        if r.set_session:
            resp.set_cookie(COOKIE, r.set_session, max_age=int(self.config.cookie_max_age), path="/", secure=True,
                            httponly=True, samesite="strict")
        if r.clear_session:
            resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return resp

    # ---- handlers ------------------------------------------------------------------------------

    def _me(self, who):
        u = self.cp._user(who.user_id)
        return {"user_id": who.user_id, "name": u["name"], "email": u["email"], "role": who.role,
                "capabilities": sorted(who.capabilities), "unlocked": self.cp.is_unlocked(who),
                "limits": {"max_concurrent": u["max_concurrent"], "max_kept": u["max_kept"],
                           "kept_until": u["kept_until"], "ttl_days": self.cp.limits.ttl_days}}

    def static_routes(self) -> List[Route]:
        """Serve the console. Only files that existed at startup under index.html, js/ and
        css/ are served, from a fixed table: a request path is looked up, never joined, so
        there is no traversal to get wrong, and tests/ and everything else stay private."""
        if not self.config.console_dir:
            return []
        root = Path(self.config.console_dir).resolve()
        table: Dict[str, Tuple[bytes, str]] = {}
        files = [root / "index.html"]
        for d in STATIC_DIRS:
            files += sorted((root / d).glob("*"))
        for f in files:
            if f.is_file() and f.suffix in STATIC_TYPES:
                table["/" + f.relative_to(root).as_posix()] = (f.read_bytes(), STATIC_TYPES[f.suffix])
        if "/index.html" not in table:
            raise ValueError(f"{root} has no index.html")
        table["/"] = table["/index.html"]

        async def serve(request: Request) -> Response:
            hit = table.get(request.url.path)
            if request.method not in ("GET", "HEAD") or not hit:
                return self._finish(Result({"error": "not_found"}, 404))
            body, ctype = hit
            resp = Response(body if request.method == "GET" else b"", media_type=ctype)
            for k, v in SECURITY_HEADERS.items():
                resp.headers[k] = v
            resp.headers["Content-Security-Policy"] = PAGE_CSP
            resp.headers["Content-Length"] = str(len(body))
            return resp
        return [Route(path, serve, methods=["GET", "HEAD"]) for path in table]

    def routes(self) -> List[Route]:
        a, cp, r = self.auth, self.cp, self.route
        enroll_rate, login_rate = (8, 60.0), (30, 60.0)
        return [
            Route("/healthz", lambda request: self._finish(Result({"ok": True})), methods=["GET"]),
            # --- authentication
            r("/api/enroll/begin", ["POST"], lambda w, d, p, q: a.begin_enrollment(
                str(d.get("invite", "")), str(d.get("name", "")), str(d.get("email", ""))), public=True, rate=enroll_rate),
            r("/api/enroll/finish", ["POST"], self._finish_enroll, public=True, rate=enroll_rate),
            r("/api/login/begin", ["POST"], lambda w, d, p, q: a.begin_login(), public=True, rate=login_rate),
            r("/api/login/finish", ["POST"], self._finish_login, public=True, rate=login_rate),
            r("/api/logout", ["POST"], lambda w, d, p, q: self._logout(q)),
            r("/api/me", ["GET"], lambda w, d, p, q: self._me(w)),
            # --- the user's key container and unlocking
            r("/api/privatedata", ["GET"], lambda w, d, p, q: {"container": (lambda c: b64e(c) if c else None)(cp.get_privatedata(w))}),
            r("/api/privatedata", ["PUT"], lambda w, d, p, q: cp.set_privatedata(w, b64d(d.get("container"))) or {"ok": True}),
            r("/api/unlock", ["POST"], lambda w, d, p, q: {"unlocked": bool(a.unlock(
                q.cookies.get(COOKIE), b64d(d.get("main_key")), d.get("ttl")).session_id)}, rate=login_rate),
            # --- passkeys
            r("/api/passkeys", ["GET"], lambda w, d, p, q: {"passkeys": a.list_passkeys(w)}),
            r("/api/passkeys/begin", ["POST"], lambda w, d, p, q: a.begin_add_passkey(w)),
            r("/api/passkeys/finish", ["POST"], lambda w, d, p, q: a.finish_add_passkey(
                w, str(d.get("ceremony_id", "")), d.get("credential") or {}, str(d.get("label", "")))),
            r("/api/passkeys/{passkey_id}", ["DELETE"], lambda w, d, p, q: a.remove_passkey(w, p["passkey_id"]) or {"ok": True}),
            # --- configs
            r("/api/schema", ["GET"], lambda w, d, p, q: cp.schema()),
            r("/api/templates", ["GET"], lambda w, d, p, q: {"templates": cp.templates(w)}),
            r("/api/configs/validate", ["POST"], lambda w, d, p, q: {"problems": cp.validate_config(w, d.get("config") or {})}),
            r("/api/configs", ["GET"], lambda w, d, p, q: {"configs": cp.list_configs(w)}),
            r("/api/configs/{name}", ["GET"], lambda w, d, p, q: {"config": cp.get_config(w, p["name"])}),
            r("/api/configs/{name}", ["PUT"], lambda w, d, p, q: cp.save_config(w, p["name"], d.get("config") or {})),
            r("/api/configs/{name}", ["DELETE"], lambda w, d, p, q: cp.delete_config(w, p["name"]) or {"ok": True}),
            # --- instances
            r("/api/instances", ["GET"], lambda w, d, p, q: {"instances": cp.list_instances(w)}),
            r("/api/instances", ["POST"], lambda w, d, p, q: Result(cp.create_instance(
                w, config=d.get("config"), config_name=d.get("config_name"), name=str(d.get("name", "")),
                keep=bool(d.get("keep", False))), 202)),
            r("/api/instances/{iid}", ["GET"], lambda w, d, p, q: cp.get_instance(w, p["iid"])),
            r("/api/instances/{iid}", ["DELETE"], lambda w, d, p, q: cp.destroy_instance(w, p["iid"])),
            r("/api/instances/{iid}/credentials", ["GET"], lambda w, d, p, q: cp.instance_credentials(w, p["iid"])),
            r("/api/instances/{iid}/stop", ["POST"], lambda w, d, p, q: cp.stop_instance(w, p["iid"])),
            r("/api/instances/{iid}/start", ["POST"], lambda w, d, p, q: cp.start_instance(w, p["iid"])),
            r("/api/instances/{iid}/reset", ["POST"], lambda w, d, p, q: Result(cp.reset_instance(w, p["iid"]), 202)),
            r("/api/instances/{iid}/keep", ["POST"], lambda w, d, p, q: cp.set_keep(w, p["iid"], bool(d.get("keep", True)))),
            # --- admin (a non-admin gets a plain 404)
            r("/api/admin/invites", ["POST"], lambda w, d, p, q: {"token": cp.create_invite(
                w, capabilities=d.get("capabilities") or [], role=str(d.get("role", "member")), email=str(d.get("email", "")),
                days_valid=float(d.get("days_valid", 7)), max_concurrent=d.get("max_concurrent"), max_kept=int(d.get("max_kept", 0)),
                kept_for_days=d.get("kept_for_days"))}, admin=True),
            r("/api/admin/invites", ["GET"], lambda w, d, p, q: {"invites": cp.list_invites(w)}, admin=True),
            r("/api/admin/invites/{prefix}", ["DELETE"], lambda w, d, p, q: cp.revoke_invite(w, p["prefix"]) or {"ok": True}, admin=True),
            r("/api/admin/users/{uid}/grant", ["POST"], lambda w, d, p, q: cp.grant(
                w, p["uid"], capabilities=d.get("capabilities"), max_concurrent=d.get("max_concurrent"),
                max_kept=d.get("max_kept"), kept_for_days=d.get("kept_for_days")) or {"ok": True}, admin=True),
            r("/api/admin/users/{uid}/disable", ["POST"], lambda w, d, p, q: (cp.disable_user(w, p["uid"]), self.oauth.revoke_user(p["uid"])) and {"ok": True}, admin=True),
            r("/api/admin/instances", ["GET"], lambda w, d, p, q: {"instances": cp.list_instances(w, all_users=True)}, admin=True),
            r("/api/admin/audit", ["GET"], lambda w, d, p, q: {"audit": (cp._require_admin(w) or cp.db.audit_log(
                min(int(q.query_params.get("limit", 100)), 500)))}, admin=True),
        ] + self._mcp_routes() + self.static_routes()

    def _mcp_routes(self):
        from .mcp_web import McpWeb
        return McpWeb(self, self.oauth).routes()

    def _finish_enroll(self, who, data, params, request):
        user, token = self.auth.finish_enrollment(str(data.get("ceremony_id", "")), data.get("credential") or {})
        return Result({"user_id": user.user_id, "next": "unlock"}, 201, set_session=token)

    def _finish_login(self, who, data, params, request):
        user, token = self.auth.finish_login(str(data.get("ceremony_id", "")), data.get("credential") or {})
        return Result({"user_id": user.user_id, "next": "unlock"}, set_session=token)

    def _logout(self, request):
        self.auth.logout(request.cookies.get(COOKIE))
        return Result({"ok": True}, clear_session=True)


def create_app(cp: ControlPlane, auth: AuthService, config: ApiConfig = None) -> Starlette:
    return Starlette(routes=Api(cp, auth, config).routes())
