#!/usr/bin/env python3
"""fixtures/create-pki.sh: SIGNING_CERT_VERIFIER_HOST re-issues the signing certificate with the verifier's dNSName
from the EXISTING key and root.

The verifier signs its OpenID4VP request objects with the same certificate the issuer uses as its DS certificate, and
its client_id is `x509_san_dns:<host>`. The re-issue must therefore never touch the key or the root: the PDP's trust
and every credential already issued depend on them."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "fixtures" / "create-pki.sh"
ISSUER = "https://issuer.example"
HOST = "verifier.example"

NEEDS = unittest.skipUnless(shutil.which("openssl") and shutil.which("bash"), "needs openssl and bash")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Pki:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())

    def run(self, **env):
        proc = subprocess.run(["bash", str(SCRIPT)], cwd=SCRIPT.parent, capture_output=True, text=True,
                              env={**os.environ, "PKI_DIR_OVERRIDE": str(self.dir), **env})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    def sans(self) -> str:
        out = subprocess.run(["openssl", "x509", "-in", str(self.dir / "signing_ec.crt"), "-noout", "-ext", "subjectAltName"],
                             capture_output=True, text=True, check=True).stdout
        return out.splitlines()[-1].strip()

    def verifies(self) -> bool:
        return subprocess.run(["openssl", "verify", "-CAfile", str(self.dir / "rootCA.crt"), str(self.dir / "signing_ec.crt")],
                              capture_output=True).returncode == 0


@NEEDS
class VerifierSan(unittest.TestCase):
    def setUp(self):
        self.pki = Pki()
        self.addCleanup(shutil.rmtree, self.pki.dir, ignore_errors=True)
        self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER)       # an environment deployed before the verifier SAN existed
        self.key, self.root = digest(self.pki.dir / "signing_ec_private.pem"), digest(self.pki.dir / "rootCA.crt")
        self.old_cert = digest(self.pki.dir / "signing_ec.crt")

    def test_the_existing_certificate_has_no_verifier_dns_name(self):
        self.assertNotIn(f"DNS:{HOST}", self.pki.sans())

    def test_reissue_adds_the_dns_name_and_keeps_the_key_and_the_root(self):
        out = self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST=HOST)
        self.assertIn("Re-issuing", out)
        sans = self.pki.sans()
        self.assertIn(f"DNS:{HOST}", sans)
        self.assertIn(f"URI:{ISSUER}", sans, "the issuer URI the PDP's mdociaca allowlist matches on must survive")
        self.assertIn("DNS:vc-issuer", sans)
        self.assertNotEqual(digest(self.pki.dir / "signing_ec.crt"), self.old_cert, "a new leaf")
        self.assertEqual(digest(self.pki.dir / "signing_ec_private.pem"), self.key, "the key must not rotate")
        self.assertEqual(digest(self.pki.dir / "rootCA.crt"), self.root, "the root must not change")
        self.assertTrue(self.pki.verifies(), "the new leaf chains to the unchanged root")
        chain = (self.pki.dir / "signing_ec_chain.pem").read_text()
        self.assertEqual(chain, (self.pki.dir / "signing_ec.crt").read_text() + (self.pki.dir / "rootCA.crt").read_text())

    def test_the_public_key_is_unchanged_so_old_signatures_still_verify(self):
        before = (self.pki.dir / "signing_ec_public.pem").read_text()
        self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST=HOST)
        leaf_pub = subprocess.run(["openssl", "x509", "-in", str(self.pki.dir / "signing_ec.crt"), "-noout", "-pubkey"],
                                  capture_output=True, text=True, check=True).stdout
        self.assertEqual(leaf_pub, before)

    def test_a_certificate_that_already_has_it_is_left_alone(self):
        self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST=HOST)
        issued = digest(self.pki.dir / "signing_ec.crt")
        out = self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST=HOST)
        self.assertIn("already current", out)
        self.assertEqual(digest(self.pki.dir / "signing_ec.crt"), issued)

    def test_unset_leaves_an_existing_certificate_alone(self):
        out = self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER)
        self.assertIn("already current", out)
        self.assertEqual(digest(self.pki.dir / "signing_ec.crt"), self.old_cert)

    def test_dedicated_verifier_key_is_separate_and_stable(self):
        verifier_key = self.pki.dir / "verifier_ec_private.pem"
        verifier_cert = self.pki.dir / "verifier_ec.crt"
        self.assertNotEqual(digest(verifier_key), self.key)
        self.assertEqual(
            subprocess.run(["openssl", "verify", "-CAfile", str(self.pki.dir / "rootCA.crt"), str(verifier_cert)],
                           capture_output=True).returncode,
            0,
        )
        self.assertEqual(
            (self.pki.dir / "verifier_ec_chain.pem").read_text(),
            verifier_cert.read_text() + (self.pki.dir / "rootCA.crt").read_text(),
        )
        key_before, cert_before = digest(verifier_key), digest(verifier_cert)
        self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER)
        self.assertEqual(digest(verifier_key), key_before)
        self.assertEqual(digest(verifier_cert), cert_before)

    def test_a_longer_host_ending_in_the_same_name_is_not_a_match(self):
        """`x.verifier.example` must not satisfy `verifier.example`, and `.` must not act as a wildcard."""
        self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST="x.verifier.example")
        out = self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST=HOST)
        self.assertIn("Re-issuing", out)
        self.assertIn(f"DNS:{HOST}", self.pki.sans())
        out = self.pki.run(SIGNING_CERT_ISSUER_URL=ISSUER, SIGNING_CERT_VERIFIER_HOST="verifierXexample")
        self.assertIn("Re-issuing", out, "an unescaped '.' would have matched verifier.example")


class DeployPassThrough(unittest.TestCase):
    def generate(self, values):
        from sirosid_core import deploy
        from sirosid_core.naming import Naming

        seen = {}

        class Fly:
            def exec(self, cmd, cwd=None, env=None):
                seen["env"] = env

            def app_exists(self, app):
                return False

        spec = type("S", (), {"values": values})()
        ctx = deploy.DeployContext(fly=Fly(), naming=Naming(env="gdc"), resources=type("R", (), {"fixtures": Path("/x")})(),
                                   spec=spec, out_dir=Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, ctx.out_dir, ignore_errors=True)
        deploy.generate_pki(ctx)
        return seen["env"]

    def test_opted_in_environment_hands_the_script_the_verifiers_public_host(self):
        env = self.generate({"verifier": {"signingCertDnsSan": True}})
        self.assertEqual(env["SIGNING_CERT_VERIFIER_HOST"], "sirosid-gdc-vc-verifier.fly.dev",
                         "the host is the client_id's x509_san_dns value")
        self.assertEqual(env["SIGNING_CERT_ISSUER_URL"], "https://sirosid-gdc-vc-apigw.fly.dev")

    def test_every_other_environment_is_unchanged(self):
        for values in ({}, {"verifier": {}}, {"verifier": {"signingCertDnsSan": False}}, None):
            self.assertNotIn("SIGNING_CERT_VERIFIER_HOST", self.generate(values), values)

    def test_gdc_and_its_nym_auth_derivative_opt_in_and_nothing_else_does(self):
        import yaml
        for env_file in sorted((ROOT / "environments").glob("*.yaml")):
            cfg = yaml.safe_load(env_file.read_text()) or {}
            on = (((cfg.get("values") or {}).get("verifier") or {}).get("signingCertDnsSan"))
            self.assertEqual(bool(on), env_file.stem in ("gdc", "nymauth"), env_file.name)


if __name__ == "__main__":
    unittest.main()
