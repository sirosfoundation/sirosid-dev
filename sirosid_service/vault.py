"""Sealing users' own data under a key only the user's passkey can produce.

The model (see the privatedata spec the wallet uses): each user has ONE main
AES-256-GCM key. In the browser it is wrapped once per passkey, the wrapping key
coming from that credential's WebAuthn PRF output - so no single passkey, and not
the server, is needed to add another. The server stores the wrapped container as an
opaque blob (it never parses it) and never sees a PRF output.

What the server does need is the main key WHILE the user is logged in, because it
acts on their configs and secrets (it deploys what their config says). So the
browser unlocks the container with the passkey and hands the server the main key for
the session. `SessionKeys` holds it in memory only, with an expiry; it is never
written anywhere. Everything the server stores for the user is sealed under it.

Consequences, deliberately:
  * work that needs only plaintext metadata (stop, start, destroy, the reaper, the
    sweeper) keeps running with nobody logged in;
  * work that needs the user's data (deploy, reset, reading a config or an
    instance's credentials) needs a live unlocked session, and raises Locked;
  * an admin, a database dump or a backup in the object store yields no user data.

Sealed blobs are bound to who and what they belong to (AAD), so a blob cannot be
copied to another user or another object and still open.
"""
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
KEY_CHECK_PLAINTEXT = b"sirosid key check v1"
DEFAULT_SESSION_TTL = 8 * 3600.0
MAX_SESSION_TTL = 24 * 3600.0


class Locked(Exception):
    """The operation needs the user's data and there is no unlocked session."""


class VaultError(Exception):
    """A blob did not open: wrong key, wrong owner or object, or tampered."""


def aad(user_id: str, kind: str, object_id: str = "") -> bytes:
    """Binds a blob to its owner and what it is. Changing any part makes it unopenable."""
    return f"sirosid/v{FORMAT_VERSION}|{user_id}|{kind}|{object_id}".encode()


class Sealer:
    """AES-256-GCM with a version byte and a fresh random nonce per blob."""

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
            raise ValueError(f"the main key must be {KEY_BYTES} bytes")
        self._aead = AESGCM(bytes(key))

    def seal(self, plaintext: bytes, associated: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return bytes([FORMAT_VERSION]) + nonce + self._aead.encrypt(nonce, bytes(plaintext), associated)

    def open(self, blob: bytes, associated: bytes) -> bytes:
        blob = bytes(blob)
        if len(blob) < 1 + NONCE_BYTES + 16 or blob[0] != FORMAT_VERSION:
            raise VaultError("not a sealed blob of a version this service understands")
        try:
            return self._aead.decrypt(blob[1:1 + NONCE_BYTES], blob[1 + NONCE_BYTES:], associated)
        except InvalidTag:
            raise VaultError("could not open: wrong key, wrong owner or object, or tampered") from None


@dataclass
class _Entry:
    sealer: Sealer
    user_id: str
    expires: float


class SessionKeys:
    """Main keys of logged-in users, in memory only.

    Nothing here is persisted: a restart locks everyone, which is the point. (Python
    cannot reliably wipe an immutable bytes object; the protection is that the key
    never leaves this process's memory, not that it is zeroised.)
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._entries: Dict[str, _Entry] = {}

    def put(self, user_id: str, key: bytes, ttl: Optional[float] = None) -> str:
        ttl = DEFAULT_SESSION_TTL if ttl is None else min(float(ttl), MAX_SESSION_TTL)
        if ttl <= 0:
            raise ValueError("a session needs a positive lifetime")
        self.purge()
        sid = "s_" + secrets.token_urlsafe(24)
        self._entries[sid] = _Entry(Sealer(key), user_id, self._clock() + ttl)
        return sid

    def get(self, session_id: str, user_id: str) -> Sealer:
        e = self._entries.get(session_id or "")
        if not e or e.expires <= self._clock():
            self._entries.pop(session_id or "", None)
            raise Locked("your session is locked: unlock it with your passkey")
        if e.user_id != user_id:
            raise Locked("this session belongs to someone else")
        return e.sealer

    def drop(self, session_id: str):
        self._entries.pop(session_id or "", None)

    def drop_user(self, user_id: str):
        for sid in [s for s, e in self._entries.items() if e.user_id == user_id]:
            del self._entries[sid]

    def purge(self):
        now = self._clock()
        for sid in [s for s, e in self._entries.items() if e.expires <= now]:
            del self._entries[sid]

    def __len__(self):
        self.purge()
        return len(self._entries)
