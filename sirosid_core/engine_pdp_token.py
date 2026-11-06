"""Mint the Rust WMP engine's PDP bearer: an AS-issued token go-wallet-backend's
/v1/evaluate and /v1/resolve TAC gate accepts (pkg/middleware/tokenauth.go's
MustHaveTAC + RequireAudience).

The engine (siros-wallet-core engine-server) evaluates trust through the
backend's proxy routes, which are TAC-gated: the bearer must be signed by
the AS's own key (the JWKS at {backend}/auth/.well-known/jwks.json), carry
`tac` covering `r`, and an audience the deployment accepts (the proxy's
RequireAudience: wallet-backend or wallet-registry). This module mints
exactly that from the environment's AS signing key - the same key the AS
signs its own tokens with, so the JWKS verifies it.

Two callers:
  - scripts/mint_engine_pdp_token.py (the CLI form, for a local stack or
    a manual Fly secret);
  - sirosid_core/deploy.py's wmp-engine branch (the deploy mints fresh
    each redeploy - the AS key is long-lived, the token's exp is not).
"""
import base64
import json
import time


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint_engine_pdp_token(signing_key_pem: str, issuer: str, audience: str = "wallet-backend",
                          ttl_hours: int = 24 * 365) -> str:
    """The engine's PDP bearer, ES256-signed with the AS's own P-256 key.

    issuer: the iss claim - the AS's issuer (the wallet-proxy origin on
    Fly, localhost:8080 locally), NOT the JWKS URL.
    """
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    except ImportError as e:  # pragma: no cover - the repo's .venv carries it
        raise SystemExit(f"minting needs the 'cryptography' package: {e}") from e

    key = load_pem_private_key(signing_key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("the AS signing key is not an EC P-256 key")

    # The kid: the RFC 7638 thumbprint form the SIROS AS keys its JWKS by
    # (base64url of SHA-256 over the JWK's required members, in lexicographic
    # order) - see fixtures/create-pki.sh's own kid derivation.
    pub_numbers = key.public_key().public_numbers()
    coordinate_len = (pub_numbers.x.bit_length() + 7) // 8
    jwk = {"crv": "P-256", "kty": "EC", "x": b64u(pub_numbers.x.to_bytes(coordinate_len, "big")),
           "y": b64u(pub_numbers.y.to_bytes(coordinate_len, "big"))}
    import hashlib
    thumbprint = hashlib.sha256(json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()).digest()
    kid = b64u(thumbprint)
    now = int(time.time())
    header = {"alg": "ES256", "kid": kid, "typ": "JWT"}
    claims = {
        "iss": issuer,
        "sub": "engine-pdp",
        "aud": audience,
        # The proxy routes require read (r) - the trust evaluation's own TAC.
        "tac": "r",
        "iat": now,
        "exp": now + ttl_hours * 3600,
    }
    signing = f"{b64u(json.dumps(header, separators=(',', ':')).encode())}.{b64u(json.dumps(claims, separators=(',', ':')).encode())}"
    der = key.sign(signing.encode(), ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return signing + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))