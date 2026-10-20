#!/usr/bin/env python3
"""nym_auth's seed-backed ssc (SUNET/vc#793/#821): the chart must give the issuer-apigw - and only it - a seed
source with a usable secret, and the credential types the commitment block. An image without #793 ignores
`apigw.seeds` silently and issues a credential with no ssc, so what we render is the only line of defence."""
import base64
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")


def rendered(secrets_dir=None):
    tmp = Path(tempfile.mkdtemp())
    out = {}
    try:
        render("compose", ROOT / "chart", resources=Resources(ROOT), out_dir=tmp / "out",
               secrets_dir=secrets_dir or tmp / "secrets", env_values={}, say=lambda *a, **k: None, mongo_password="x")
        for f in ("vc-apigw", "vc-issuer", "vc-verifier", "vc-registry"):
            out[f] = yaml.safe_load((tmp / "out" / f"{f}.yaml").read_text())
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class SeedCommitments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = rendered(cls.tmp / "secrets")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_apigw_has_a_derived_seed_with_a_usable_secret(self):
        seed = self.cfg["vc-apigw"]["apigw"]["seeds"]["primary"]
        self.assertEqual(seed["model"], "derived")
        raw = base64.urlsafe_b64decode(seed["secret"])      # base64url, as vc reads it
        self.assertGreaterEqual(len(raw), 32, "vc refuses a seed secret under 32 bytes decoded")

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

    def test_the_seed_secret_is_permanent_across_renders(self):
        again = rendered(self.tmp / "secrets")
        self.assertEqual(again["vc-apigw"]["apigw"]["seeds"]["primary"]["secret"],
                         self.cfg["vc-apigw"]["apigw"]["seeds"]["primary"]["secret"],
                         "a replaced seed secret repseudonymises everyone")


if __name__ == "__main__":
    unittest.main()
