#!/usr/bin/env python3
"""nym_auth's seed-backed ssc (SUNET/vc#793/#821): the chart must give the issuer-apigw - and only it - a seed
source with a usable secret, and the credential types the commitment block. An image without #793 ignores
`apigw.seeds` silently and issues a credential with no ssc, so what we render is the only line of defence."""
import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core import vc_render  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")


# What environments/gdc.yaml switches on (the default is off: it needs a vc image with SUNET/vc#793/#821).
GDC_VALUES = {"issuer": {"apigw": {"seeds": {"primary": {"model": "derived"}}, "credentialEncryption": {"enabled": True}}}}


def rendered(secrets_dir=None, env_values=None, documents=False):
    tmp = Path(tempfile.mkdtemp())
    out = {}
    try:
        render("compose", ROOT / "chart", resources=Resources(ROOT), out_dir=tmp / "out",
               secrets_dir=secrets_dir or tmp / "secrets", env_values=env_values or {}, say=lambda *a, **k: None,
               mongo_password="x")
        for f in ("vc-apigw", "vc-issuer", "vc-verifier", "vc-registry"):
            out[f] = yaml.safe_load((tmp / "out" / f"{f}.yaml").read_text())
        if documents:
            out["documents"] = {p.name: json.loads(p.read_text()) for p in (tmp / "out" / "documents").glob("nym_auth*.json")}
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class SeedCommitments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = rendered(cls.tmp / "secrets", env_values=GDC_VALUES)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_apigw_has_a_derived_seed_whose_secret_is_a_file_not_config(self):
        seed = self.cfg["vc-apigw"]["apigw"]["seeds"]["primary"]
        self.assertEqual(seed["model"], "derived")
        self.assertEqual(seed["secret_path"], "/seeds/primary")
        self.assertNotIn("secret", seed, "vc reads `secret` only from its secrets file; inlined it is not found (and would sit in `flyctl config show`)")
        raw = base64.urlsafe_b64decode((self.tmp / "secrets" / vc_render.seed_secret_file("primary")).read_text())
        self.assertGreaterEqual(len(raw), 32, "vc refuses a seed secret under 32 bytes decoded")

    def test_every_seed_can_be_mounted_by_the_single_machine_layout(self):
        from sirosid_core import singlemachine
        for name in self.cfg["vc-apigw"]["apigw"]["seeds"]:
            path = self.cfg["vc-apigw"]["apigw"]["seeds"][name]["secret_path"]
            self.assertIn(path, singlemachine.SECRET_PATHS["vc"], f"{name}: the single-machine layout cannot mount {path}")
            self.assertIn(f"vc-secrets/{vc_render.seed_secret_file(name)}", [s for s, _ in singlemachine.SECRETS.values()], name)

    def test_nym_auth_and_its_twin_commit_to_the_same_delivered_seed(self):
        meta = self.cfg["vc-apigw"]["common"]["credential_metadata"]
        want = {"ssc": {"seed": "primary", "subjects": ["issuing_country", "personal_administrative_number"],
                        "deliver": True}}
        self.assertEqual(meta["nym_auth"]["commitments"], want)
        self.assertEqual(meta["nym_auth_offer"]["commitments"], want)
        self.assertNotIn("commitments", meta["pid_1_8"])

    def test_only_the_apigw_is_given_seeds_or_commitments(self):
        for name in ("vc-issuer", "vc-verifier", "vc-registry"):
            cfg = self.cfg[name]
            self.assertNotIn("seeds", cfg.get("apigw") or {}, name)
            for scope, meta in (cfg.get("common", {}).get("credential_metadata") or {}).items():
                self.assertNotIn("commitments", meta, f"{name}/{scope}")

    def test_the_response_metadata_echo_stays_off_until_vc_has_the_aliasing_fix(self):
        """vc 6c3ac47/c123c32 can write a delivered seed over the PUBLISHED issuer metadata when
        apigw.delivery.emit_credential_metadata_on_response is true (fixed in SUNET/vc#821 52e4ad2db). It
        defaults to false and nothing here may switch it on while gdc runs an image without that fix."""
        delivery = self.cfg["vc-apigw"]["apigw"].get("delivery") or {}
        self.assertFalse(delivery.get("emit_credential_metadata_on_response", False))

    def test_the_issuer_publishes_credential_encryption_from_a_mounted_key(self):
        """A wallet asked to receive a delivered seed refuses an issuer that advertises no
        credential_response_encryption (found on the first Pixel run, 2026-10-20)."""
        enc = self.cfg["vc-apigw"]["apigw"]["issuer_metadata"]["credential_encryption"]
        self.assertEqual([k["private_key_path"] for k in enc["keys"]], [vc_render.CREDENTIAL_ENCRYPTION_PATH])
        self.assertFalse(enc["response_encryption_required"], "wallets that do no JWE must keep working")
        pem = (self.tmp / "secrets" / vc_render.CREDENTIAL_ENCRYPTION_FILE).read_text()
        self.assertIn("EC PRIVATE KEY", pem)
        for name in ("vc-issuer", "vc-verifier", "vc-registry"):
            self.assertNotIn("credential_encryption", (self.cfg[name].get("apigw") or {}).get("issuer_metadata") or {})

    def test_the_single_machine_layout_can_mount_the_encryption_key(self):
        from sirosid_core import singlemachine
        self.assertIn(vc_render.CREDENTIAL_ENCRYPTION_PATH, singlemachine.SECRET_PATHS["vc"])
        self.assertIn(f"vc-secrets/{vc_render.CREDENTIAL_ENCRYPTION_FILE}", [s for s, _ in singlemachine.SECRETS.values()])

    def test_the_issuer_advertises_its_notification_endpoint(self):
        meta = self.cfg["vc-apigw"]["apigw"]["issuer_metadata"]
        self.assertTrue(meta["notification_endpoint"].endswith("/notification"))

    def test_the_seed_secret_is_permanent_across_renders(self):
        before = (self.tmp / "secrets" / vc_render.seed_secret_file("primary")).read_text()
        rendered(self.tmp / "secrets", env_values=GDC_VALUES)
        self.assertEqual((self.tmp / "secrets" / vc_render.seed_secret_file("primary")).read_text(), before,
                         "a replaced seed secret repseudonymises everyone")


if __name__ == "__main__":
    unittest.main()


@NEEDS
class WithoutSeedSupport(unittest.TestCase):
    """The default: an environment whose vc image predates SUNET/vc#793 (the shared Fly pin is one) ignores
    `apigw.seeds` silently and would issue nym_auth with no ssc at all, so nothing is rendered for it and the
    documents keep the legacy public test ssc."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = rendered(cls.tmp / "secrets", documents=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_seeds_commitments_or_encryption_are_rendered(self):
        apigw = self.cfg["vc-apigw"]["apigw"]
        self.assertNotIn("seeds", apigw)
        self.assertNotIn("credential_encryption", apigw["issuer_metadata"])
        for scope, meta in self.cfg["vc-apigw"]["common"]["credential_metadata"].items():
            self.assertNotIn("commitments", meta, scope)
        self.assertEqual([p.name for p in (self.tmp / "secrets").iterdir()
                          if p.name.startswith("vcApigwSeed") and p.stat().st_size], [], "no real seed secret")

    def test_compose_still_has_files_for_its_bind_mounts(self):
        """docker-compose.vc-services.yml mounts both unconditionally; a missing source becomes a DIRECTORY."""
        for name in (vc_render.seed_secret_file("primary"), vc_render.CREDENTIAL_ENCRYPTION_FILE):
            path = self.tmp / "secrets" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.stat().st_size, 0, f"{name} is only a placeholder while the feature is off")

    def test_the_nym_auth_documents_carry_the_legacy_ssc(self):
        self.assertEqual(set(self.cfg["documents"]), {"nym_auth.json", "nym_auth_offer.json"})
        for name, docs in self.cfg["documents"].items():
            for holder, doc in docs.items():
                self.assertEqual(doc["document_data"]["ssc"], vc_render.LEGACY_NYM_AUTH_SSC, f"{name}/{holder}")

    def test_the_source_fixtures_never_carry_it(self):
        for name in ("nym_auth.json", "nym_auth_offer.json"):
            for doc in json.loads((ROOT / "fixtures" / "vc-bootstrapping" / name).read_text()).values():
                self.assertNotIn("ssc", doc["document_data"], "an environment with seeds is refused a pre-set ssc")

    def test_the_legacy_ssc_is_the_documented_public_test_commitment(self):
        import hashlib
        self.assertEqual(base64.b64decode(vc_render.LEGACY_NYM_AUTH_SSC),
                         hashlib.sha256(bytes(range(16))).digest())
