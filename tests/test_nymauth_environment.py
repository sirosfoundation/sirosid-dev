#!/usr/bin/env python3
"""environments/nymauth.yaml: the nym_auth-only test environment derived from gdc (docs/NYMAUTH-TESTING.md).

It must stay a SMALL diff against gdc - same chart values and the same proven image pins, only its own hostnames - so the
team can see exactly what is nymauth-specific, and everything nymauth-critical (verifier_id equal to the prefixed
client_id of ITS OWN verifier, the zkp_req formats and presets, the PDP verifier_ids policies, the seed/credential
encryption switches) must be set."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import env_config  # noqa: E402
from sirosid_core import vc_render  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")
HOST = "sirosid-nymauth-vc-verifier.fly.dev"


def swap(value):
    return json.loads(json.dumps(value).replace("sirosid-gdc-vc-verifier", "sirosid-nymauth-vc-verifier"))


class SmallDiffAgainstGdc(unittest.TestCase):
    def setUp(self):
        self.gdc = env_config.load_environment_config("gdc")
        self.ny = env_config.load_environment_config("nymauth")

    def test_values_are_gdcs_with_only_the_verifier_host_changed(self):
        self.assertEqual(self.ny["values"], swap(self.gdc["values"]))

    def test_the_images_are_the_same_proven_pins(self):
        gdc_images = dict(self.gdc["images"])
        gdc_images.pop("siros-verifier")  # gdc-only additive verifier
        gdc_images.pop("env-admin")      # gdc-only local build with additive reset support
        self.assertEqual(self.ny["images"], gdc_images)

    def test_what_a_nym_auth_test_does_not_need_is_dropped_and_nothing_else(self):
        for key in ("trusted_issuers", "trusted_verifiers", "trusted_verifier_roots"):
            self.assertEqual(self.ny[key], [], key)
        self.assertEqual(self.ny["rical_provider_url"], "")
        for key in ("region", "credential_registries", "zk_circuits_sources", "android_apps", "wallet_attestation"):
            self.assertEqual(self.ny[key], self.gdc[key], key)


@NEEDS
class RenderedNymAuthEnvironment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = env_config.load_environment_config("nymauth")
        tmp = Path(tempfile.mkdtemp())
        try:
            render("fly", ROOT / "chart", env="nymauth", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
                   env_values=cfg["values"], naming=Naming(env="nymauth"), say=lambda *a, **k: None, mongo_password="x")
            out = tmp / "o" / "fly-nymauth"
            cls.cfg = {n: yaml.safe_load((out / f"{n}.yaml").read_text()) for n in ("vc-verifier", "vc-apigw", "vc-issuer", "pdp")}
            cls.vctms = sorted(p.name for p in (out / "vctms").glob("nym_auth*"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_id_is_the_prefixed_client_id_of_its_own_host(self):
        verifier = self.cfg["vc-verifier"]["verifier"]
        self.assertEqual(verifier["public_url"], f"https://{HOST}")
        self.assertEqual(verifier["nym_auth"]["verifier_id"], f"x509_san_dns:{HOST}")
        self.assertTrue(verifier["nym_auth"]["enable"])

    def test_the_zkp_req_formats_presets_and_pdp_are_set(self):
        verifier = self.cfg["vc-verifier"]["verifier"]
        self.assertEqual(verifier["preferred_vp_formats"]["zkp_req"],
                         {"system_id_values": ["urn:circuits:longfellow:nym_auth:v1", "urn:circuits:vega:nym_auth:v1"],
                          "proof_system_values": ["longfellow-libzk-v1", "vega-v1"]})
        for preset in ("nym_auth_pseudonym", "nym_auth_pseudonym_age"):
            self.assertEqual(verifier["presets"][preset]["credentials"]["nym_auth"]["format"], "zkp_req")
        self.assertEqual(verifier["trust"]["pdp_url"], "http://sirosid-nymauth-pdp.internal:8080")

    def test_the_pdp_returns_verifier_ids_for_the_verifier_actions_only(self):
        policies = self.cfg["pdp"]["policies"]["policies"]
        self.assertEqual(set(policies), {"credential-verifier", "verifier"})
        whitelist = self.cfg["pdp"]["registries"]["whitelist"]["lists"]
        self.assertEqual(whitelist["verifiers"], ["https://sirosid-nymauth-vc-apigw.fly.dev", f"https://{HOST}"])

    def test_the_issuer_delivers_the_opening_encrypted_with_a_notification_endpoint(self):
        apigw = self.cfg["vc-apigw"]["apigw"]
        self.assertNotIn("seeds", apigw)
        self.assertEqual(vc_render.opening_secret_paths(apigw), ["/seeds/nym_auth_seed.key"])
        self.assertIn("credential_encryption", apigw["issuer_metadata"])
        self.assertEqual(apigw["issuer_metadata"]["notification_endpoint"], "https://sirosid-nymauth-vc-apigw.fly.dev/notification")
        meta = self.cfg["vc-apigw"]["common"]["credential_metadata"]
        self.assertNotIn("nym_auth_offer", meta, "no twins: the pinned vc images carry SUNET/vc#840")
        self.assertTrue(self.cfg["vc-apigw"]["apigw"]["data_sources"]["datastore"]["scopes"]["nym_auth"]["allow_preauth_offer"])

    def test_no_other_environments_hostname_leaks_into_the_configs(self):
        for name, cfg in self.cfg.items():
            text = json.dumps(cfg)
            for stale in ("sirosid-gdc-vc", "sirosid-gdc-pdp", "sirosid-gdc-wallet", "sirosid-gdc-mongodb", "sirosid-gdc-mini"):
                self.assertNotIn(stale, text, f"{name}: {stale}")


if __name__ == "__main__":
    unittest.main()
