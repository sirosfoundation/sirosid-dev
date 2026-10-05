"""Passkey authentication: enrolment, login, web sessions and unlocking.

Four things happen here, in this order for a new user:

  1. ENROL  - an invite plus a passkey ceremony creates the account. The invite is
              checked before the ceremony and spent only if it succeeds.
  2. LOGIN  - a discoverable-credential ceremony proves who the user is and starts a
              web session. That alone only reaches metadata; the session is LOCKED.
  3. UNLOCK - the browser used the passkey's PRF output (computed client-side, with
              the constant salt this service advertises) to open the user's key
              container and hands over the main key; ControlPlane.begin_session keeps
              it in memory. See vault.py.
  4. USE    - ControlPlane operations, with a Principal that carries the key's session.

The PRF output never reaches the server, and the server cannot check that an
authenticator supports PRF (the client reports it unsigned): the browser must refuse
to enrol one that does not, and a user whose passkey cannot unlock simply cannot use
their data.

Verification is webauthn (py_webauthn); everything else is here: single-use,
short-lived ceremonies, sign-count checks, origin and RP ID pinning, hashed web
session tokens.
"""
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from .db import hash_token
from .service import ControlPlane, Forbidden, InvalidInvite, NotFound, NotSignedIn, Principal, ServiceError

CHALLENGE_TTL = 300.0
WEB_SESSION_TTL = 12 * 3600.0
MAX_PASSKEYS = 10


class AuthError(ServiceError):
    """A ceremony failed. The message is safe to show; details are not leaked."""


@dataclass(frozen=True)
class AuthConfig:
    rp_id: str = "console.sirosid.dev"
    rp_name: str = "SIROS ID Dev"
    origins: Tuple[str, ...] = ("https://console.sirosid.dev",)
    challenge_ttl: float = CHALLENGE_TTL
    web_session_ttl: float = WEB_SESSION_TTL

    @property
    def prf_salt(self) -> bytes:
        """One constant salt for every credential: the authenticator already derives a
        different PRF output per credential, so a per-credential salt adds nothing, and
        a constant one lets the browser ask for it before it knows which passkey will
        be used (discoverable login)."""
        return hashlib.sha256(b"sirosid-prf-v1|" + self.rp_id.encode()).digest()


@dataclass
class _Ceremony:
    kind: str
    challenge: bytes
    expires: float
    data: dict = field(default_factory=dict)


class AuthService:
    def __init__(self, cp: ControlPlane, config: AuthConfig = None):
        self.cp = cp
        self.db = cp.db
        self.config = config or AuthConfig()
        self.clock = cp.clock
        self._ceremonies: Dict[str, _Ceremony] = {}
        self._unlocked: Dict[str, str] = {}        # web session token hash -> ControlPlane session id

    # ---- ceremonies (single use, short lived, in memory) ----------------------------

    def _open_ceremony(self, kind, challenge, **data) -> str:
        now = self.clock()
        for k in [k for k, c in self._ceremonies.items() if c.expires <= now]:
            del self._ceremonies[k]
        if len(self._ceremonies) > 5000:
            raise AuthError("too many ceremonies in flight; try again shortly")
        cid = secrets.token_urlsafe(24)
        self._ceremonies[cid] = _Ceremony(kind, challenge, now + self.config.challenge_ttl, data)
        return cid

    def _take(self, cid: str, kind: str) -> _Ceremony:
        c = self._ceremonies.pop(cid or "", None)           # popped first: a ceremony is never reusable
        if not c or c.kind != kind or c.expires <= self.clock():
            raise AuthError("this sign-in attempt expired or was already used; start again")
        return c

    def _options(self, opts) -> dict:
        d = json.loads(options_to_json(opts))
        d.setdefault("extensions", {})["prf"] = {"eval": {"first": bytes_to_base64url(self.config.prf_salt)}}
        return d

    # ---- enrolment ---------------------------------------------------------------------

    def begin_enrollment(self, invite_token: str, name: str, email: str = "") -> dict:
        self.cp.check_invite(invite_token, email)
        name = (name or "").strip()[:80] or "user"
        user_handle = secrets.token_bytes(16)
        opts = generate_registration_options(
            rp_id=self.config.rp_id, rp_name=self.config.rp_name, user_id=user_handle, user_name=email or name,
            user_display_name=name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        cid = self._open_ceremony("enroll", opts.challenge, invite=invite_token, name=name, email=email, handle=user_handle)
        return {"ceremony_id": cid, "options": self._options(opts)}

    def finish_enrollment(self, ceremony_id: str, credential: dict) -> Tuple[Principal, str]:
        c = self._take(ceremony_id, "enroll")
        verified = self._verify_registration(credential, c.challenge)
        self.cp.check_invite(c.data["invite"], c.data["email"])           # still valid after the ceremony
        user = self.cp.redeem_invite(c.data["invite"], c.data["name"], c.data["email"])
        self._store_credential(user.user_id, verified, credential, label="first passkey")
        self.db.audit(user.user_id, "enroll", user.user_id)
        return user, self.create_web_session(user.user_id)

    def _verify_registration(self, credential: dict, challenge: bytes):
        try:
            return verify_registration_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.config.rp_id,
                expected_origin=list(self.config.origins), require_user_verification=True)
        except (InvalidRegistrationResponse, ValueError, KeyError, TypeError):
            raise AuthError("the passkey could not be verified") from None

    def _store_credential(self, user_id, verified, credential, label=""):
        cid = bytes_to_base64url(verified.credential_id)
        if self.db.one("SELECT 1 AS x FROM credentials WHERE id=?", (cid,)):
            raise AuthError("this passkey is already registered")
        transports = (credential.get("response") or {}).get("transports") or []
        self.db.execute("INSERT INTO credentials(id,user_id,public_key,sign_count,transports,label,created_at) VALUES(?,?,?,?,?,?,?)",
                        (cid, user_id, verified.credential_public_key, verified.sign_count, json.dumps(list(transports)),
                         label[:60], self.clock()))

    # ---- login ----------------------------------------------------------------------------

    def begin_login(self) -> dict:
        opts = generate_authentication_options(rp_id=self.config.rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        return {"ceremony_id": self._open_ceremony("login", opts.challenge), "options": self._options(opts)}

    def finish_login(self, ceremony_id: str, credential: dict) -> Tuple[Principal, str]:
        c = self._take(ceremony_id, "login")
        try:
            cid = credential["id"]
        except (KeyError, TypeError):
            raise AuthError("the passkey could not be verified") from None
        row = self.db.one("SELECT * FROM credentials WHERE id=?", (cid,))
        if not row:
            raise AuthError("the passkey could not be verified")           # same message as a bad signature
        try:
            verified = verify_authentication_response(
                credential=credential, expected_challenge=c.challenge, expected_rp_id=self.config.rp_id,
                expected_origin=list(self.config.origins), credential_public_key=bytes(row["public_key"]),
                credential_current_sign_count=row["sign_count"], require_user_verification=True)
        except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError):
            self.db.audit(row["user_id"], "login_refused", cid[:12])
            raise AuthError("the passkey could not be verified") from None
        user = self.cp._user(row["user_id"])
        if user["disabled"]:
            raise Forbidden("this account is disabled")
        self.db.execute("UPDATE credentials SET sign_count=?, last_used=? WHERE id=?", (verified.new_sign_count, self.clock(), cid))
        self.db.audit(user["id"], "login", user["id"])
        return self.cp.principal_for(user["id"]), self.create_web_session(user["id"])

    # ---- more passkeys -------------------------------------------------------------------------

    def begin_add_passkey(self, who: Principal) -> dict:
        user = self.cp._user(who.user_id)
        existing = self.db.all("SELECT id FROM credentials WHERE user_id=?", (who.user_id,))
        if len(existing) >= MAX_PASSKEYS:
            raise AuthError(f"at most {MAX_PASSKEYS} passkeys")
        opts = generate_registration_options(
            rp_id=self.config.rp_id, rp_name=self.config.rp_name, user_id=who.user_id.encode(),
            user_name=user["email"] or user["name"], user_display_name=user["name"],
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["id"])) for r in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
        cid = self._open_ceremony("add", opts.challenge, user=who.user_id)
        return {"ceremony_id": cid, "options": self._options(opts)}

    def finish_add_passkey(self, who: Principal, ceremony_id: str, credential: dict, label: str = "") -> dict:
        c = self._take(ceremony_id, "add")
        if c.data["user"] != who.user_id:
            raise AuthError("this sign-in attempt belongs to someone else")
        verified = self._verify_registration(credential, c.challenge)
        self._store_credential(who.user_id, verified, credential, label=label or "passkey")
        self.db.audit(who.user_id, "add_passkey", who.user_id)
        return {"id": bytes_to_base64url(verified.credential_id)}

    def list_passkeys(self, who: Principal) -> list:
        return [{"id": r["id"], "label": r["label"], "created_at": r["created_at"], "last_used": r["last_used"]}
                for r in self.db.all("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at", (who.user_id,))]

    def remove_passkey(self, who: Principal, credential_id: str):
        rows = self.db.all("SELECT id FROM credentials WHERE user_id=?", (who.user_id,))
        if credential_id not in [r["id"] for r in rows]:
            raise NotFound("no such passkey")
        if len(rows) == 1:
            raise AuthError("you cannot remove your only passkey")
        self.db.execute("DELETE FROM credentials WHERE id=?", (credential_id,))
        self.db.audit(who.user_id, "remove_passkey", credential_id[:12])

    # ---- web sessions ---------------------------------------------------------------------------

    def create_web_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
        self.db.execute("INSERT INTO web_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                        (hash_token(token), user_id, now, now + self.config.web_session_ttl))
        return token

    def principal_from_token(self, token: Optional[str]) -> Principal:
        """The caller for a web session token; raises Forbidden if it is missing,
        expired, or belongs to a disabled account. Unlocked only if the user unlocked
        THIS web session and the key has not expired."""
        if not token:
            raise NotSignedIn("not signed in")
        h = hash_token(token)
        row = self.db.one("SELECT * FROM web_sessions WHERE token_hash=?", (h,))
        if not row or row["expires_at"] <= self.clock():
            raise NotSignedIn("not signed in")
        sid = self._unlocked.get(h, "")
        try:
            return self.cp.principal_for(row["user_id"], sid)
        except NotFound:
            raise NotSignedIn("not signed in") from None

    def unlock(self, token: str, main_key: bytes, ttl: float = None) -> Principal:
        who = self.principal_from_token(token)
        unlocked = self.cp.begin_session(who.user_id, main_key, ttl)
        self._unlocked[hash_token(token)] = unlocked.session_id
        return unlocked

    def logout(self, token: Optional[str]):
        if not token:
            return
        h = hash_token(token)
        sid = self._unlocked.pop(h, "")
        if sid:
            self.cp.keys.drop(sid)
        self.db.execute("DELETE FROM web_sessions WHERE token_hash=?", (h,))
