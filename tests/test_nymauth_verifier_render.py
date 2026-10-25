#!/usr/bin/env python3
"""The gdc verifier's nym_auth block (SUNET/vc#832): what the rendered verifier config must say, and the one value
that is easy to get wrong silently.

`verifier_id` scopes every pseudonym, and a wrong one is not an error anyone sees: it yields a pseudonym no other
implementation reports. The artifact digest has a placeholder lookalike (docs/CONFIGURATION.md's example keeps the
real digest's first 8 and last 4 hex characters and zeroes the middle), so the check below is on the whole value."""
import re
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
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")

# zk-cred-longfellow src/nymauth/profile.rs - the authoritative source (the native loader hashes the decompressed
# artifact and compares against exactly these).
PROFILE = {
    "hash_circuit_id": "5f8c75ae5c3893ef6ab6a079628cb385ba85e95a85437d58570c455289d75729",
    "sig_circuit_id": "00513797c75b2eab1a9db75004977c15317d4e40c0db1d00674523431baa75cb",
    "block_enc_hash": 6983,
    "block_enc_sig": 4679,
    "artifact_bytes": 1062842,
    "artifact_sha256": "92dd42fdf99673249eec33c7d07850bd9da66fbdab01ef26391e04c8341c1b7f",
}
# The Vega entry: the shapes' DECOMPRESSED digest and size, the catalog's nymauth-vega-mdoc-v1 (zk-circuits-test.fly.dev);
# a Vega entry carries no Longfellow parameter (vc refuses one).
VEGA = {
    "system_id": "urn:circuits:vega:nym_auth:v1",
    "config": "known-offender-matching-ssc",
    "artifact_path": "/etc/vc/circuits/nym_auth-vega-ssc.bin",
    "artifact_bytes": 12205144,
    "artifact_sha256": "4da5e73afb9be2682e70be793c9956a73304e21f31246aa10da0d3e95c926425",
}


def render_gdc_verifier():
    tmp = Path(tempfile.mkdtemp())
    try:
        values = env_config.load_environment_config("gdc").get("values") or {}
        naming = Naming(env="gdc")
        render("fly", ROOT / "chart", env="gdc", resources=Resources(ROOT), out_dir=tmp / "out", secrets_dir=tmp / "secrets",
               env_values=values, naming=naming, say=lambda *a, **k: None, mongo_password="x")
        text = (tmp / "out" / "fly-gdc" / "vc-verifier.yaml").read_text()
        return text, yaml.safe_load(text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class GdcNymAuthVerifier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text, cfg = render_gdc_verifier()
        cls.full = cfg
        cls.cfg = cfg["verifier"]

    def test_enabled_with_exactly_the_profile_the_circuit_ships(self):
        entry = self.cfg["nym_auth"]["circuits"]["allow"]
        self.assertEqual(len(entry), 2)
        self.assertEqual({k: entry[0][k] for k in PROFILE}, PROFILE)
        self.assertEqual(entry[0]["system_id"], "urn:circuits:longfellow:nym_auth:v1")
        self.assertEqual(entry[0]["config"], "known-offender-matching-ssc")
        self.assertEqual(entry[1], VEGA)
        self.assertTrue(self.cfg["nym_auth"]["enable"])

    def test_numbers_are_integers_in_the_file_vc_reads(self):
        """Helm renders 1062842 as 1.062842e+06 and PyYAML dumps that back as 1062842.0; a Go int field refuses it."""
        for k in ("artifact_bytes", "block_enc_hash", "block_enc_sig"):
            self.assertIs(type(self.cfg["nym_auth"]["circuits"]["allow"][0][k]), int, k)
        self.assertRegex(self.text, r"artifact_bytes: 1062842\n")

    def test_the_digest_is_not_the_documentation_placeholder(self):
        digest = self.cfg["nym_auth"]["circuits"]["allow"][0]["artifact_sha256"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotRegex(digest, r"0{8}", "a run of zeros is the doc_example placeholder, not a SHA-256")

    def test_ids_are_strings_not_numbers_the_yaml_could_reinterpret(self):
        entry = self.cfg["nym_auth"]["circuits"]["allow"][0]
        for k in ("hash_circuit_id", "sig_circuit_id", "artifact_sha256"):
            self.assertIsInstance(entry[k], str, k)

    def test_verifier_id_is_the_published_client_id(self):
        """Rulebook v0.4: verifier_id is the client_id exactly as published, x509_san_dns:<host of public_url>, prefix
        included (the bare host was the rule before v0.4 and is the NOT-a-verifier_id value now: it yields a
        pseudonym no other implementation reports). Changing it changes every pseudonym."""
        vid = self.cfg["nym_auth"]["verifier_id"]
        self.assertEqual(vid, "x509_san_dns:" + re.sub(r"^https://", "", self.cfg["public_url"]))

    def test_the_artifact_path_is_where_the_overlay_image_puts_it(self):
        self.assertEqual(self.cfg["nym_auth"]["circuits"]["allow"][0]["artifact_path"], "/etc/vc/circuits/nym_auth-ssc.bin")
        dockerfile = (ROOT / "dockerfiles" / "verifier-nymauth.Dockerfile").read_text()
        self.assertIn("/etc/vc/circuits/nym_auth-ssc.bin", dockerfile)
        self.assertIn(PROFILE["artifact_sha256"], dockerfile)
        self.assertIn(str(PROFILE["artifact_bytes"]), dockerfile)
        # the second artifact is baked in under the path the Vega entry names, with its size and digest checked at build time
        self.assertIn(VEGA["artifact_path"].rsplit("/", 1)[1], dockerfile)
        self.assertIn(VEGA["artifact_sha256"], dockerfile)
        self.assertIn(str(VEGA["artifact_bytes"]), dockerfile)

    def test_a_pdp_is_configured_because_the_verifier_refuses_to_start_without_one(self):
        self.assertTrue(self.cfg["trust"]["pdp_url"], "nym_auth takes the issuer key from the holder's chain; no PDP = refuse to start")

    def test_the_request_may_name_only_what_this_verifier_offers(self):
        zk = self.cfg["preferred_vp_formats"]["zkp_req"]
        self.assertEqual(zk["system_id_values"], ["urn:circuits:longfellow:nym_auth:v1", "urn:circuits:vega:nym_auth:v1"])
        self.assertEqual(zk["proof_system_values"], ["longfellow-libzk-v1", "vega-v1"])
        # vc does not check this at startup: an offered system without an allow entry is refused at presentation time
        self.assertEqual({e["system_id"] for e in self.cfg["nym_auth"]["circuits"]["allow"]}, set(zk["system_id_values"]))
        self.assertIn("dc+sd-jwt", self.cfg["preferred_vp_formats"], "adding zkp_req must not drop the existing formats")

    def test_the_nym_auth_presets_are_shaped_as_vc_reads_them(self):
        """vc's brief (SUNET/vc#832): preset key `req` (the WIRE member `_req` was silently accepted and left empty),
        item key fn_id, ver and prf required, no source_format / doctype_value (both derive from the scope's
        credential_metadata, so a scope key that is not that entry reproduces invalid_meta in every wallet)."""
        presets = self.cfg["presets"]
        scopes = self.full["common"]["credential_metadata"]
        for label in ("nym_auth_pseudonym", "nym_auth_pseudonym_age"):
            self.assertEqual(list(presets[label]["credentials"]), ["nym_auth"], label)
            self.assertIn("nym_auth", scopes, "the preset's scope key must be a common.credential_metadata scope")
            self.assertEqual(scopes["nym_auth"]["format"], "mso_mdoc")
            cred = presets[label]["credentials"]["nym_auth"]
            self.assertEqual((cred["format"], cred["validate"]), ("zkp_req", "nym_auth"))
            self.assertNotIn("_req", cred)
            for banned in ("source_format", "doctype_value"):
                self.assertNotIn(banned, cred, banned)
            first = cred["req"][0]
            self.assertEqual((first["fn_id"], first["params"]["ver"], first["params"]["prf"]), ("pseudonym", 1, "sha-256"))
            self.assertEqual(first["params"]["config"], "known-offender-matching-ssc")
            for item in cred["req"]:
                self.assertIn("fn_id", item)
                self.assertNotIn("id", item, "`id` was the pre-v0.4 item key")
        ages = [i for i in presets["nym_auth_pseudonym_age"]["credentials"]["nym_auth"]["req"] if i["fn_id"] == "age_check"]
        self.assertEqual(len(ages), 1)
        self.assertEqual(len(presets["nym_auth_pseudonym"]["credentials"]["nym_auth"]["req"]), 1)

    def test_no_pre_v0_4_names_remain(self):
        """vc fails startup on circuit_zk / circuit_id_values / circuit_proof_system_values, naming the replacement."""
        text = self.text
        for old in ("circuit_zk", "circuit_id_values", "circuit_proof_system_values"):
            self.assertNotIn(old, text, old)


if __name__ == "__main__":
    unittest.main()
