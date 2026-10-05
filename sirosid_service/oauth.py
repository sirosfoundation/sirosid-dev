"""A minimal OAuth 2.1 authorization server, only so MCP clients can be given access.

What it is for: a user (already signed in to the console with their passkey and unlocked)
lets an application - Claude Code, claude.ai, an editor - manage THEIR instances through
the MCP endpoint. What it deliberately is not: a general identity provider. The only
login is the console's passkey login; this module never sees a credential.

Shape (RFC 6749/7636/7591/8414/8707/9728, MCP authorization spec 2025-06-18):
  * public clients only, registered dynamically (RFC 7591), authorization code + PKCE S256
    (mandatory), no refresh tokens, no client secrets;
  * the browser step is a consent screen in the console, so the passkey login, the unlock and
    the CSRF defences are the console's own - /authorize only parks the request and redirects;
  * approving mints a SEPARATE key session (SessionKeys.clone): the application never gets the
    main key, only a bearer token that stands for a handle on the server-held key, with its own
    lifetime and revocable from the console. When that key session ends - expiry, revocation,
    a server restart - the token stops working, so token validity IS key validity;
  * codes, pending requests and tokens live in memory only (like the keys they unlock);
    registered clients are the one thing stored. A restart therefore asks every application to
    sign in again, which is the same thing a restart does to every console session.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .db import hash_token
from .service import ControlPlane, NotFound, Principal, ServiceError
from .vault import Locked

SCOPE = "sirosid"
PENDING_TTL = 600.0
CODE_TTL = 60.0
TOKEN_TTL = 8 * 3600.0
MAX_CLIENTS = 500
MAX_REDIRECTS = 5
MAX_PENDING = 2000
MAX_TOKENS_PER_USER = 25


class OAuthError(Exception):
    """An RFC 6749 error: `code` is the standard error name, `status` the HTTP status."""

    def __init__(self, code: str, description: str = "", status: int = 400):
        super().__init__(description or code)
        self.code, self.description, self.status = code, description, status


def valid_redirect_uri(uri: str) -> bool:
    """https anywhere, or http only to a loopback address (native apps, RFC 8252). No
    fragments, no credentials, no other schemes (javascript:, data:, custom schemes)."""
    try:
        u = urlsplit(uri)
        host = u.hostname or ""
        u.port                                              # raises on a bad port
    except ValueError:
        return False
    if u.fragment or u.username or u.password or not host or len(uri) > 512:
        return False
    if u.scheme == "https":
        return True
    if u.scheme == "http":
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return False


def same_redirect(registered: str, given: str) -> bool:
    """Exact match - except a loopback redirect may change its PORT (RFC 8252 7.3)."""
    if registered == given:
        return True
    a, b = urlsplit(registered), urlsplit(given)
    loop = a.scheme == "http" and (a.hostname == "localhost" or _is_loopback_ip(a.hostname))
    return loop and (a.scheme, a.hostname, a.path, a.query) == (b.scheme, b.hostname, b.path, b.query)


def _is_loopback_ip(host) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class _Pending:
    client_id: str
    redirect_uri: str
    state: str
    challenge: str
    resource: str
    expires: float


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    resource: str
    user_id: str
    session_id: str
    expires: float


@dataclass
class _Token:
    client_id: str
    user_id: str
    session_id: str
    created: float
    expires: float
    code_hash: str = ""


class OAuthService:
    def __init__(self, cp: ControlPlane, issuer: str):
        self.cp, self.db, self.clock = cp, cp.db, cp.clock
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self._pending: Dict[str, _Pending] = {}
        self._codes: Dict[str, _Code] = {}
        self._used_codes: Dict[str, str] = {}                # code hash -> token hash, to revoke on replay
        self._tokens: Dict[str, _Token] = {}

    # ---- discovery ----------------------------------------------------------------------------

    def server_metadata(self) -> dict:
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/oauth/authorize",
                "token_endpoint": self.issuer + "/oauth/token", "registration_endpoint": self.issuer + "/oauth/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [SCOPE]}

    def resource_metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.issuer], "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"]}

    # ---- dynamic client registration (RFC 7591) -----------------------------------------------

    def register_client(self, meta: dict) -> dict:
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECTS or not all(isinstance(u, str) and valid_redirect_uri(u) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be 1-5 https URLs or http loopback URLs")
        method = meta.get("token_endpoint_auth_method", "none")
        if method != "none":
            raise OAuthError("invalid_client_metadata", "only public clients (token_endpoint_auth_method none) are supported")
        grants = meta.get("grant_types", ["authorization_code"])
        if not isinstance(grants, list) or any(g != "authorization_code" for g in grants):
            raise OAuthError("invalid_client_metadata", "only the authorization_code grant is supported")
        name = " ".join(str(meta.get("client_name") or "").split())[:80] or "an application"
        if self.db.one("SELECT COUNT(*) AS n FROM oauth_clients")["n"] >= MAX_CLIENTS:
            raise OAuthError("temporarily_unavailable", "too many registered applications", 503)
        cid = "c_" + secrets.token_urlsafe(18)
        self.db.execute("INSERT INTO oauth_clients(client_id,name,redirect_uris,created_at) VALUES(?,?,?,?)",
                        (cid, name, json.dumps(uris), self.clock()))
        return {"client_id": cid, "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"], "response_types": ["code"], "scope": SCOPE}

    def _client(self, client_id: str) -> dict:
        r = self.db.one("SELECT * FROM oauth_clients WHERE client_id=?", (client_id or "",))
        if not r:
            raise OAuthError("invalid_client", "unknown client", 401)
        return {"id": r["client_id"], "name": r["name"], "redirect_uris": json.loads(r["redirect_uris"])}

    # ---- /authorize: park the request, the console asks the user -----------------------------

    def begin_authorization(self, params: dict) -> str:
        """Validate and park an authorization request; returns the id the console shows.
        Errors here are NEVER redirected to the client (the redirect URI is not yet trusted)."""
        client = self._client(params.get("client_id", ""))
        redirect = params.get("redirect_uri", "")
        if not any(same_redirect(r, redirect) for r in client["redirect_uris"]):
            raise OAuthError("invalid_request", "redirect_uri is not registered for this client")
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "only response_type=code")
        if params.get("code_challenge_method") != "S256" or not 43 <= len(params.get("code_challenge", "")) <= 128:
            raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required")
        resource = params.get("resource", "")
        if resource and resource != self.resource:
            raise OAuthError("invalid_target", f"this server's resource is {self.resource}")
        scope = params.get("scope", "")
        if scope and set(scope.split()) - {SCOPE}:
            raise OAuthError("invalid_scope", f"the only scope is {SCOPE}")
        self._purge()
        if len(self._pending) >= MAX_PENDING:
            raise OAuthError("temporarily_unavailable", "too many pending requests", 503)
        pid = secrets.token_urlsafe(24)
        self._pending[pid] = _Pending(client["id"], redirect, params.get("state", "")[:512], params["code_challenge"],
                                      resource, self.clock() + PENDING_TTL)
        return pid

    def describe_pending(self, pid: str) -> dict:
        p = self._get_pending(pid)
        client = self._client(p.client_id)
        u = urlsplit(p.redirect_uri)
        return {"client_name": client["name"], "redirect_host": u.netloc,
                "redirect_uri": p.redirect_uri, "expires_in": int(p.expires - self.clock()), "scope": SCOPE}

    def _get_pending(self, pid: str) -> _Pending:
        p = self._pending.get(pid or "")
        if not p or p.expires <= self.clock():
            self._pending.pop(pid or "", None)
            raise NotFound("this authorization request expired; start again from the application")
        return p

    def approve(self, who: Principal, pid: str) -> str:
        """The signed-in, UNLOCKED user says yes. Returns the URL to send the browser to."""
        p = self._get_pending(pid)
        sid = self.cp.share_session(who, TOKEN_TTL)          # raises Locked unless unlocked
        del self._pending[pid]
        code = secrets.token_urlsafe(32)
        self._codes[hash_token(code)] = _Code(p.client_id, p.redirect_uri, p.challenge, p.resource, who.user_id, sid,
                                              self.clock() + CODE_TTL)
        self.db.audit(who.user_id, "oauth_approve", p.client_id)
        return self._redirect(p, code=code)

    def deny(self, who: Principal, pid: str) -> str:
        p = self._get_pending(pid)
        del self._pending[pid]
        self.db.audit(who.user_id, "oauth_deny", p.client_id)
        return self._redirect(p, error="access_denied")

    def _redirect(self, p: _Pending, **q) -> str:
        if p.state:
            q["state"] = p.state
        q["iss"] = self.issuer                                # RFC 9207: lets the client detect a mix-up
        u = urlsplit(p.redirect_uri)
        query = urlencode(parse_qsl(u.query, keep_blank_values=True) + list(q.items()))
        return urlunsplit((u.scheme, u.netloc, u.path, query, ""))

    # ---- /token --------------------------------------------------------------------------------

    def token(self, form: dict) -> dict:
        if form.get("grant_type") != "authorization_code":
            raise OAuthError("unsupported_grant_type", "only authorization_code")
        chash = hash_token(form.get("code", ""))
        if chash in self._used_codes:                        # replay: the code's tokens die with it (RFC 6749 4.1.2)
            self._tokens.pop(self._used_codes.pop(chash), None)
            raise OAuthError("invalid_grant", "this code was already used")
        c = self._codes.pop(chash, None)
        if not c or c.expires <= self.clock():
            raise OAuthError("invalid_grant", "the code is invalid or expired")
        if not hmac.compare_digest(form.get("client_id", ""), c.client_id):
            raise OAuthError("invalid_grant", "the code was issued to another client")
        if form.get("redirect_uri", "") != c.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        verifier = form.get("code_verifier", "")
        if not 43 <= len(verifier) <= 128 or not hmac.compare_digest(pkce_challenge(verifier), c.challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        resource = form.get("resource", "")
        if resource and resource != self.resource:
            raise OAuthError("invalid_target", f"this server's resource is {self.resource}")
        try:
            expires = min(self.cp.session_expiry(c.session_id, c.user_id), self.clock() + TOKEN_TTL)
        except Locked:
            raise OAuthError("invalid_grant", "the user's session ended before the code was redeemed") from None
        self._cap_tokens(c.user_id)
        tok = secrets.token_urlsafe(32)
        th = hash_token(tok)
        self._tokens[th] = _Token(c.client_id, c.user_id, c.session_id, self.clock(), expires, chash)
        self._used_codes[chash] = th
        if len(self._used_codes) > 5000:
            self._used_codes = dict(list(self._used_codes.items())[-2500:])
        return {"access_token": tok, "token_type": "Bearer", "expires_in": int(expires - self.clock()), "scope": SCOPE}

    def _cap_tokens(self, user_id: str):
        mine = sorted((t.created, h) for h, t in self._tokens.items() if t.user_id == user_id)
        for _, h in mine[:max(0, len(mine) - MAX_TOKENS_PER_USER + 1)]:
            self._revoke_hash(h)

    # ---- bearer tokens ---------------------------------------------------------------------------

    def principal_for_token(self, token: str) -> Principal:
        """The user a bearer token acts for, with the key session behind it. Raises OAuthError
        (401, invalid_token) when the token is unknown, expired, revoked or its key session is gone."""
        t = self._tokens.get(hash_token(token or ""))
        if not t:
            raise OAuthError("invalid_token", "unknown or revoked token", 401)
        if t.expires <= self.clock() or not self.cp.session_alive(t.session_id, t.user_id):
            self._revoke_hash(hash_token(token))
            raise OAuthError("invalid_token", "the token expired", 401)
        try:
            return self.cp.principal_for(t.user_id, t.session_id)
        except ServiceError:
            raise OAuthError("invalid_token", "the account is not available", 401) from None

    # ---- the user's connected applications (console) ------------------------------------------------

    def grants(self, who: Principal) -> List[dict]:
        self._purge()
        out = []
        for h, t in self._tokens.items():
            if t.user_id == who.user_id:
                out.append({"id": h[:16], "client_name": self._client_name(t.client_id), "created_at": t.created, "expires_at": t.expires})
        return sorted(out, key=lambda g: g["created_at"], reverse=True)

    def _client_name(self, cid: str) -> str:
        try:
            return self._client(cid)["name"]
        except OAuthError:
            return "unknown application"

    def revoke(self, who: Principal, grant_id: str):
        hits = [h for h, t in self._tokens.items() if t.user_id == who.user_id and h.startswith(grant_id or "x")]
        if len(hits) != 1:
            raise NotFound("no such connected application")
        self._revoke_hash(hits[0])
        self.db.audit(who.user_id, "oauth_revoke", grant_id)

    def revoke_user(self, user_id: str):
        for h in [h for h, t in self._tokens.items() if t.user_id == user_id]:
            self._revoke_hash(h)

    def _revoke_hash(self, h: str):
        t = self._tokens.pop(h, None)
        if t:
            self.cp.drop_session(t.session_id)               # the handle on the key dies with the token

    def _purge(self):
        now = self.clock()
        for k in [k for k, p in self._pending.items() if p.expires <= now]:
            del self._pending[k]
        for k in [k for k, c in self._codes.items() if c.expires <= now]:
            del self._codes[k]
        for h in [h for h, t in self._tokens.items() if t.expires <= now]:
            self._revoke_hash(h)
