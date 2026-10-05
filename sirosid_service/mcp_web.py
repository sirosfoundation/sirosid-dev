"""HTTP routes for OAuth and MCP. Thin: OAuthService and McpServer hold the logic.

Two kinds of caller, two kinds of routes:
  * the console (cookie, Origin allow-list, CSRF rules of Api.route): consent, connected apps;
  * applications (no cookie, no Origin - they are not browsers): discovery, registration, the
    token endpoint and /mcp, authenticated by what they hold (a code + PKCE verifier, a bearer
    token). These answer to rate limits instead, and /mcp refuses a browser Origin that is not
    ours, which is the MCP spec's defence against DNS rebinding.
"""
import json
import logging
from typing import List
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .api import MAX_BODY, Result, status_for
from .mcp import MAX_MESSAGE, McpServer
from .oauth import OAuthError, OAuthService
from .service import ServiceError

log = logging.getLogger("sirosid.mcp")


class McpWeb:
    def __init__(self, api, oauth: OAuthService):
        self.api, self.oauth = api, oauth
        self.mcp = McpServer(api.cp)

    # ---- helpers -----------------------------------------------------------------------------

    def _reply(self, payload, status=200, headers=None) -> Response:
        resp = self.api._finish(Result(payload, status))
        for k, v in (headers or {}).items():
            resp.headers[k] = v
        return resp

    def _oauth_error(self, e: OAuthError) -> Response:
        body = {"error": e.code}
        if e.description:
            body["error_description"] = e.description
        headers = {"WWW-Authenticate": self._challenge(e.code)} if e.status == 401 and e.code == "invalid_token" else None
        return self._reply(body, e.status, headers)

    def _challenge(self, code: str = "") -> str:
        meta = f'resource_metadata="{self.oauth.issuer}/.well-known/oauth-protected-resource"'
        return f'Bearer error="{code}", {meta}' if code else f"Bearer {meta}"

    async def _limited(self, request: Request, bucket: str, limit: int, window: float = 60.0):
        self.api.limiter.check(bucket, self.api._client(request), limit, window)

    async def _raw(self, request: Request, cap: int) -> bytes:
        raw = await request.body()
        if len(raw) > cap:
            raise OAuthError("invalid_request", "request too large", 413)
        return raw

    # ---- discovery -----------------------------------------------------------------------------

    async def server_metadata(self, request):
        return self._reply(self.oauth.server_metadata())

    async def resource_metadata(self, request):
        return self._reply(self.oauth.resource_metadata())

    # ---- registration, authorize, token --------------------------------------------------------

    async def register(self, request: Request):
        try:
            await self._limited(request, "oauth-register", 10)
            try:
                meta = json.loads(await self._raw(request, 16 * 1024))
            except ValueError:
                raise OAuthError("invalid_client_metadata", "the body must be JSON") from None
            if not isinstance(meta, dict):
                raise OAuthError("invalid_client_metadata", "the body must be a JSON object")
            return self._reply(self.oauth.register_client(meta), 201)
        except OAuthError as e:
            return self._oauth_error(e)
        except ServiceError:
            return self._reply({"error": "temporarily_unavailable", "error_description": "too many attempts; wait a moment"}, 429)

    async def authorize(self, request: Request):
        try:
            await self._limited(request, "oauth-authorize", 30)
            pid = self.oauth.begin_authorization(dict(request.query_params))
        except OAuthError as e:
            return self._oauth_error(e)                  # never redirected: the redirect URI is not trusted yet
        except ServiceError:
            return self._reply({"error": "temporarily_unavailable"}, 429)
        resp = Response(status_code=302)
        resp.headers["Location"] = f"/#authorize={pid}"
        for k, v in self.api._finish(Result({})).headers.items():
            if k.lower() not in ("content-length", "content-type"):
                resp.headers[k] = v
        return resp

    async def token(self, request: Request):
        try:
            await self._limited(request, "oauth-token", 30)
            if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
                raise OAuthError("invalid_request", "send application/x-www-form-urlencoded")
            form = dict(parse_qsl((await self._raw(request, 8 * 1024)).decode("utf-8", "replace"), keep_blank_values=True))
            return self._reply(self.oauth.token(form), headers={"Pragma": "no-cache"})
        except OAuthError as e:
            return self._oauth_error(e)
        except ServiceError:
            return self._reply({"error": "temporarily_unavailable"}, 429)

    # ---- /mcp ----------------------------------------------------------------------------------------

    async def mcp_post(self, request: Request):
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.api.config.origins:
            return self._reply({"error": "forbidden", "message": "this origin may not call the MCP endpoint"}, 403)
        try:
            await self._limited(request, "mcp", 300)
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                # RFC 6750 3.1: no credentials presented -> a bare challenge, no error code
                return self._reply({"error": "unauthorized", "error_description": "send a bearer token; see the WWW-Authenticate header"},
                                   401, {"WWW-Authenticate": self._challenge()})
            who = self.oauth.principal_for_token(auth[7:].strip())
            try:
                message = json.loads(await self._raw(request, MAX_MESSAGE))
            except ValueError:
                return self._reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
            from starlette.concurrency import run_in_threadpool
            out = await run_in_threadpool(self.mcp.handle, who, message)
        except OAuthError as e:
            return self._oauth_error(e)
        except ServiceError:
            return self._reply({"error": "rate_limited"}, 429)
        if out is None:
            return self.api._finish_empty(202)
        return self._reply(out)

    async def mcp_other(self, request: Request):
        return self._reply({"error": "method_not_allowed", "message": "this MCP server only accepts POST"}, 405, {"Allow": "POST"})

    # ---- console routes ------------------------------------------------------------------------------

    def routes(self) -> List[Route]:
        r, o = self.api.route, self.oauth
        return [
            Route("/.well-known/oauth-authorization-server", self.server_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource", self.resource_metadata, methods=["GET"]),
            Route("/.well-known/oauth-protected-resource/mcp", self.resource_metadata, methods=["GET"]),
            Route("/oauth/register", self.register, methods=["POST"]),
            Route("/oauth/authorize", self.authorize, methods=["GET"]),
            Route("/oauth/token", self.token, methods=["POST"]),
            Route("/mcp", self.mcp_post, methods=["POST"]),
            Route("/mcp", self.mcp_other, methods=["GET", "DELETE", "PUT", "PATCH"]),
            r("/api/oauth/pending/{pid}", ["GET"], lambda w, d, p, q: self._guard(lambda: o.describe_pending(p["pid"]))),
            r("/api/oauth/approve", ["POST"], lambda w, d, p, q: {"redirect": o.approve(w, str(d.get("id", "")))}),
            r("/api/oauth/deny", ["POST"], lambda w, d, p, q: {"redirect": o.deny(w, str(d.get("id", "")))}),
            r("/api/oauth/grants", ["GET"], lambda w, d, p, q: {"grants": o.grants(w)}),
            r("/api/oauth/grants/{gid}", ["DELETE"], lambda w, d, p, q: o.revoke(w, p["gid"]) or {"ok": True}),
        ]

    @staticmethod
    def _guard(fn):
        try:
            return fn()
        except OAuthError as e:
            raise ServiceError(e.description or e.code) from None
