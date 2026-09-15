#!/usr/bin/env python3
"""scripts/api_auth.py: the admin-API key, its JWKS and the tokens it signs.

    python3 -m unittest tests/test_api_auth.py

Verification goes through `openssl dgst -verify` (raw r||s converted back to
DER here) so the test needs nothing beyond what the script itself needs.
"""
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import api_auth  # noqa: E402
import datastore  # noqa: E402


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raw_to_der(raw: bytes) -> bytes:
    def integer(v: bytes) -> bytes:
        v = v.lstrip(b"\x00") or b"\x00"
        if v[0] & 0x80:
            v = b"\x00" + v
        return b"\x02" + bytes([len(v)]) + v
    body = integer(raw[:32]) + integer(raw[32:])
    return b"\x30" + bytes([len(body)]) + body


class KeyAndJWKS(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.key = api_auth.ensure_key(self.tmp / api_auth.KEY_FILENAME)

    def test_key_is_private_and_reused(self):
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        before = self.key.read_bytes()
        api_auth.ensure_key(self.key)
        self.assertEqual(before, self.key.read_bytes(), "an existing key must never be rotated in place")

    def test_jwks_shape(self):
        k = api_auth.jwks(self.key)["keys"][0]
        self.assertEqual((k["kty"], k["crv"], k["alg"], k["use"]), ("EC", "P-256", "ES256", "sig"))
        self.assertEqual(len(b64url_decode(k["x"])), 32)
        self.assertEqual(len(b64url_decode(k["y"])), 32)
        self.assertTrue(k["kid"])   # jwx's WithKeySet requires a kid by default
        self.assertEqual(k["kid"], api_auth.jwk(self.key)["kid"], "kid must be stable for the key")

    def test_token_verifies_and_carries_the_spocp_subject(self):
        tok = api_auth.mint(self.key, "sirosid-dev", "https://issuer.example", "admin@default", ttl=60)
        h, c, sig = tok.split(".")
        header = json.loads(b64url_decode(h))
        claims = json.loads(b64url_decode(c))
        self.assertEqual(header, {"alg": "ES256", "typ": "JWT", "kid": api_auth.jwk(self.key)["kid"]})
        self.assertEqual(claims["iss"], "sirosid-dev")
        self.assertEqual(claims["aud"], "https://issuer.example")
        self.assertEqual(claims["eppn"], "admin@default", "vc's extractSPOCPSubject reads eppn")
        self.assertEqual(claims["exp"] - claims["iat"], 60)

        pub = self.tmp / "pub.pem"
        pub.write_bytes(subprocess.run(["openssl", "ec", "-in", self.key, "-pubout"], check=True, capture_output=True).stdout)
        der = self.tmp / "sig.der"
        der.write_bytes(raw_to_der(b64url_decode(sig)))
        out = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", der],
                             input=f"{h}.{c}".encode(), capture_output=True)
        self.assertEqual(out.returncode, 0, (out.stdout + out.stderr).decode())

    def test_der_to_raw_pads_short_integers(self):
        # r with a leading zero byte stripped by DER, s at full length.
        r = b"\x01" * 31
        s = b"\x7f" + b"\x02" * 31
        der = b"\x30" + bytes([2 + len(r) + 2 + len(s)]) + b"\x02" + bytes([len(r)]) + r + b"\x02" + bytes([len(s)]) + s
        raw = api_auth.der_to_raw(der)
        self.assertEqual(raw, b"\x00" + r + s)


class SPOCPSubject(unittest.TestCase):
    def test_reads_the_admin_rule_the_chart_renders(self):
        rules = ["(vc (service *)(method *)(path /api/v1/*)(subject admin@default)(authentic_source *)(scope *))"]
        self.assertEqual(datastore.spocp_subject(rules), "admin@default")

    def test_refuses_without_a_concrete_subject(self):
        with self.assertRaises(SystemExit):
            datastore.spocp_subject(["(vc (service *)(method *)(path /api/v1/*)(subject *)(authentic_source *)(scope *))"])


if __name__ == "__main__":
    unittest.main()
