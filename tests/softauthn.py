"""A software WebAuthn authenticator, for tests.

Builds the same byte structures a real passkey produces (authenticator data, a COSE
ES256 key, 'none' attestation, a signed assertion), so the server's verification is
exercised for real rather than mocked. It also records what the 'browser' would do
with the PRF extension: a stable per-credential secret, as an authenticator derives.
"""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP, FLAG_UV, FLAG_AT = 0x01, 0x04, 0x40


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self, origin: str = "https://sirosid.dev", rp_id: str = "sirosid.dev"):
        self.origin, self.rp_id = origin, rp_id
        self._creds = {}          # credential id -> dict(key, count, user_handle, prf_secret)

    # ---- registration --------------------------------------------------------

    def create(self, options: dict, *, origin=None, rp_id=None, uv=True, credential_id: bytes = None) -> dict:
        origin, rp_id = origin or self.origin, rp_id or self.rp_id
        challenge = options["challenge"]
        key = ec.generate_private_key(ec.SECP256R1())
        cred_id = credential_id or os.urandom(32)       # reusing an id models re-registering the same passkey
        pub = key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: pub.x.to_bytes(32, "big"), -3: pub.y.to_bytes(32, "big")})
        flags = FLAG_UP | (FLAG_UV if uv else 0) | FLAG_AT
        auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
                     + bytes(16) + struct.pack(">H", len(cred_id)) + cred_id + cose)
        self._creds[cred_id] = {"key": key, "count": 0, "user_handle": unb64u(options["user"]["id"]),
                                "prf_secret": os.urandom(32)}
        client = json.dumps({"type": "webauthn.create", "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(cred_id), "rawId": b64u(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "attestationObject": b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {"prf": {"enabled": True}}}

    # ---- authentication ------------------------------------------------------

    def get(self, options: dict, *, credential_id: bytes = None, origin=None, rp_id=None, uv=True, bump=1,
            tamper_signature=False) -> dict:
        origin, rp_id = origin or self.origin, rp_id or self.rp_id
        cred_id = credential_id or next(iter(self._creds))
        c = self._creds[cred_id]
        c["count"] += bump
        flags = FLAG_UP | (FLAG_UV if uv else 0)
        auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", c["count"])
        client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"], "origin": origin, "crossOrigin": False}).encode()
        sig = c["key"].sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if tamper_signature:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        return {"id": b64u(cred_id), "rawId": b64u(cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client), "authenticatorData": b64u(auth_data), "signature": b64u(sig),
                             "userHandle": b64u(c["user_handle"])},
                "clientExtensionResults": {}}

    # ---- PRF (what the authenticator would compute for the browser) --------------

    def prf(self, credential_id: bytes, salt: bytes) -> bytes:
        """A stable 32-byte secret per (credential, salt): the PRF output."""
        import hmac
        return hmac.new(self._creds[credential_id]["prf_secret"], b"WebAuthn PRF\x00" + salt, hashlib.sha256).digest()

    @property
    def credential_ids(self):
        return list(self._creds)
