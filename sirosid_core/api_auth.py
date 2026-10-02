#!/usr/bin/env python3
"""The credential a caller of vc-apigw's admin/datastore API presents.

vc-apigw's `/api/v1/*` (datastore upload/search/delete, identity mappings) is
guarded by `api_server.api_auth`: a Bearer JWT validated against a JWKS the
chart mounts at /main-config/api_auth_jwks.json, then authorized by the
chart's SPOCP rule `(subject admin@<tenant.id>)`. The chart insists on
rendering that block - it refuses to ship an unauthenticated admin API - and
until 2026-09-15 this repo stripped it again because nothing here could mint
the JWT. This module is that missing half:

- `ensure_key` generates one EC P-256 key per deployment target (compose:
  fixtures/rendered-secrets/, Fly: fixtures/rendered/fly-<env>/), reused on
  every later render exactly like the other generated secrets
- `jwks` derives the public JWKS the renderer feeds the chart as
  `issuer.apiAuth.jwks.jwksData`
- `mint` signs the short-lived admin token `scripts/datastore.py` sends

Standard library plus the `openssl` CLI only (fixtures/create-pki.sh already
depends on it), so `make up` gains no Python dependency. The JWKS carries
`kid` and `alg` deliberately: vc verifies with jwx's WithKeySet, which
requires a matching kid by default and does not infer the algorithm from
the key.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

CURVE = "prime256v1"
ALG = "ES256"
# The `iss` every sirosid-dev-minted admin token carries; values-base.yaml's
# issuer.apiAuth.jwks.issuer must say the same thing.
ISSUER = "sirosid-dev"
KEY_FILENAME = "apiAuthKey.pem"
JWKS_FILENAME = "api_auth_jwks.json"
DEFAULT_TTL = 300
# Seconds of clock skew tolerated between this machine and the issuer.
IAT_LEEWAY = 30


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _openssl(*args, input: bytes = None) -> bytes:
    return subprocess.run(["openssl", *args], input=input, check=True, capture_output=True).stdout


def ensure_key(path: Path) -> Path:
    """Idempotent: an existing key is reused, never rotated in place - a
    rotation would invalidate nothing durable (tokens live minutes), but the
    JWKS baked into a running environment's config would no longer match the
    key a caller signs with until the next deploy."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = _openssl("ecparam", "-name", CURVE, "-genkey", "-noout")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return path


def public_point(path: Path) -> tuple[bytes, bytes]:
    """(x, y) of the key's public point, 32 bytes each."""
    der = _openssl("ec", "-in", str(path), "-pubout", "-outform", "DER")
    # SubjectPublicKeyInfo ends with the BIT STRING holding the uncompressed
    # point 0x04 || X || Y; for P-256 that is the last 65 bytes.
    point = der[-65:]
    if point[0] != 0x04 or len(point) != 65:
        raise ValueError(f"{path}: not an uncompressed P-256 public point")
    return point[1:33], point[33:65]


def jwk(path: Path) -> dict:
    x, y = public_point(path)
    core = {"crv": "P-256", "kty": "EC", "x": _b64url(x), "y": _b64url(y)}
    # RFC 7638 thumbprint as kid: stable for the key, no state to keep.
    thumb = hashlib.sha256(json.dumps(core, separators=(",", ":"), sort_keys=True).encode()).digest()
    return {**core, "kid": _b64url(thumb), "alg": ALG, "use": "sig"}


def jwks(path: Path) -> dict:
    return {"keys": [jwk(path)]}


def der_to_raw(sig: bytes, size: int = 32) -> bytes:
    """ECDSA-Sig-Value (SEQUENCE of two INTEGERs) -> the fixed-size r||s JWS wants."""
    if sig[0] != 0x30:
        raise ValueError("not a DER SEQUENCE")
    pos = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7F)
    out = b""
    for _ in range(2):
        if sig[pos] != 0x02:
            raise ValueError("expected DER INTEGER")
        length = sig[pos + 1]
        value = sig[pos + 2: pos + 2 + length].lstrip(b"\x00")
        out += value.rjust(size, b"\x00")
        pos += 2 + length
    return out


def mint(path: Path, issuer: str, audience: str, subject: str, ttl: int = DEFAULT_TTL) -> str:
    """A signed admin token: `eppn` is what vc's SPOCP layer reads as the
    subject (`extractSPOCPSubject`: eppn, then email); `sub` is set to the
    same value for anything else that looks."""
    path = Path(path)
    # Backdate iat a little: jwx validates iat <= now with no leeway, and a
    # caller whose clock runs a second or two ahead of the server would
    # otherwise be rejected with "iat not satisfied".
    now = int(time.time()) - IAT_LEEWAY
    header = {"alg": ALG, "typ": "JWT", "kid": jwk(path)["kid"]}
    claims = {"iss": issuer, "aud": audience, "sub": subject, "eppn": subject,
              "iat": now, "exp": now + ttl, "jti": secrets.token_urlsafe(16)}
    signing_input = b".".join(_b64url(json.dumps(p, separators=(",", ":")).encode()).encode()
                              for p in (header, claims))
    der = _openssl("dgst", "-sha256", "-sign", str(path), input=signing_input)
    return (signing_input + b"." + _b64url(der_to_raw(der)).encode()).decode()
