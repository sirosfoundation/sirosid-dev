"""The browser key container, tested two ways.

1. Its own Node tests (console/test/container.test.mjs, `node --test`).
2. A cross-implementation check: the container is opened and created here, in Python, written
   from the structure in the privatedata spec's key layer (HKDF-SHA256 -> AES-GCM unwraps a
   JWK P-256 private key -> ECDH with the container's ephemeral public key -> AES-KW unwraps
   the main key). If both sides agree, the format is a format and not an accident of one
   implementation.
"""
import base64
import glob
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "console", "test")
NODE = shutil.which("node")

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
    HAVE_CRYPTO = True
except ImportError:                                                  # pragma: no cover
    HAVE_CRYPTO = False


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def tag(b):
    return {"$b64u": b64u(b)}


def untag(t):
    return unb64u(t["$b64u"])


def _int(b):
    return int.from_bytes(b, "big")


def py_open(container, credential_id, prf):
    entry = next(k for k in container["prfKeys"] if untag(k["credentialId"]) == credential_id)
    wrapping = HKDF(hashes.SHA256(), 32, untag(entry["hkdfSalt"]), untag(entry["hkdfInfo"])).derive(prf)
    u = entry["keypair"]["privateKey"]["unwrapKey"]
    jwk = json.loads(AESGCM(wrapping).decrypt(untag(u["unwrapAlgo"]["iv"]), untag(u["wrappedKey"]), None))
    priv = ec.derive_private_key(_int(unb64u(jwk["d"])), ec.SECP256R1())
    eph = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), untag(container["mainKey"]["publicKey"]["importKey"]["keyData"]))
    return aes_key_unwrap(priv.exchange(ec.ECDH(), eph), untag(entry["unwrapKey"]["wrappedKey"]))


def py_create(credential_id, prf, prf_salt):
    main = os.urandom(32)
    priv = ec.generate_private_key(ec.SECP256R1())
    raw = lambda k: k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    pub_raw = raw(priv)
    nums = priv.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big")),
           "d": b64u(priv.private_numbers().private_value.to_bytes(32, "big")), "ext": True, "key_ops": ["deriveKey"]}
    hkdf_salt, info, iv = os.urandom(32), b"sirosid-console PRF v1", os.urandom(12)
    wrapping = HKDF(hashes.SHA256(), 32, hkdf_salt, info).derive(prf)
    wrapped_priv = AESGCM(wrapping).encrypt(iv, json.dumps(jwk).encode(), None)
    eph = ec.generate_private_key(ec.SECP256R1())
    wrapped_main = aes_key_wrap(eph.exchange(ec.ECDH(), priv.public_key()), main)
    pk = lambda r: {"importKey": {"format": "raw", "keyData": tag(r), "algorithm": {"name": "ECDH", "namedCurve": "P-256"}}}
    return {
        "format": "sirosid-keycontainer", "version": 1,
        "mainKey": {"publicKey": pk(raw(eph)),
                    "unwrapKey": {"format": "raw", "unwrapAlgo": "AES-KW", "unwrappedKeyAlgo": {"name": "AES-GCM", "length": 256}}},
        "prfKeys": [{
            "credentialId": tag(credential_id), "prfSalt": tag(prf_salt), "hkdfSalt": tag(hkdf_salt), "hkdfInfo": tag(info),
            "keypair": {"publicKey": pk(pub_raw),
                        "privateKey": {"unwrapKey": {"format": "jwk", "wrappedKey": tag(wrapped_priv),
                                                     "unwrapAlgo": {"name": "AES-GCM", "iv": tag(iv)},
                                                     "unwrappedKeyAlgo": {"name": "ECDH", "namedCurve": "P-256"}}}},
            "unwrapKey": {"wrappedKey": tag(wrapped_main),
                          "unwrappingKey": {"deriveKey": {"algorithm": {"name": "ECDH"}, "derivedKeyAlgorithm": {"name": "AES-KW", "length": 256}}}},
        }],
    }, main


def node(request):
    p = subprocess.run([NODE, os.path.join(ROOT, "tests", "console_xcheck.mjs")], input=json.dumps(request), capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class NodeTests(unittest.TestCase):
    def test_console_modules(self):
        p = subprocess.run([NODE, "--test"] + sorted(glob.glob(os.path.join(TESTS, "*.test.mjs"))), capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout[-3000:] + p.stderr[-2000:])


@unittest.skipUnless(NODE and HAVE_CRYPTO, "needs node and the cryptography package")
class CrossImplementation(unittest.TestCase):
    def setUp(self):
        self.cred, self.prf, self.salt = os.urandom(32), os.urandom(32), os.urandom(32)

    def test_python_opens_a_container_made_in_javascript(self):
        r = node({"op": "create", "credentialId": self.cred.hex(), "prfOutput": self.prf.hex(), "prfSalt": self.salt.hex()})
        self.assertEqual(py_open(r["container"], self.cred, self.prf).hex(), r["mainKey"])

    def test_javascript_opens_a_container_made_in_python(self):
        container, main = py_create(self.cred, self.prf, self.salt)
        r = node({"op": "open", "container": container, "credentialId": self.cred.hex(), "prfOutput": self.prf.hex()})
        self.assertEqual(r["mainKey"], main.hex())

    def test_the_main_key_from_a_container_seals_for_the_service(self):
        """The bytes /api/unlock receives work as the service's sealing key."""
        from sirosid_service.vault import Sealer, aad
        r = node({"op": "create", "credentialId": self.cred.hex(), "prfOutput": self.prf.hex(), "prfSalt": self.salt.hex()})
        key = bytes.fromhex(r["mainKey"])
        blob = Sealer(key).seal(b"hello", aad("u1", "config", "c1"))
        self.assertEqual(Sealer(py_open(r["container"], self.cred, self.prf)).open(blob, aad("u1", "config", "c1")), b"hello")


if __name__ == "__main__":
    unittest.main()
