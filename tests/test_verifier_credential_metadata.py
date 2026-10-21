#!/usr/bin/env python3
"""The verifier's common.credential_metadata must not list the pre-authorized offer twins (`<type>_offer`): they are
issuance-only scopes, and listing them doubled every entry of the verifier's /ui/metadata and picker. The issuer's
block keeps them (an offer's credential_configuration_id is resolved against credential_configurations_supported),
and the base scopes stay everywhere - nym_auth included, because the zkp_req presets take source_format and
doctype_value from credential_metadata[nym_auth]."""
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


def render_blocks(values):
    tmp = Path(tempfile.mkdtemp())
    try:
        render("fly", ROOT / "chart", env="t", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
               env_values=values, naming=Naming(env="t"), say=lambda *a, **k: None, mongo_password="x")
        out = tmp / "o" / "fly-t"
        return {n: yaml.safe_load((out / f"{n}.yaml").read_text()) for n in ("vc-verifier", "vc-apigw", "vc-issuer")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class VerifierHidesOfferTwins(unittest.TestCase):
    """An environment WITHOUT issuer.allowPreauthOffer still has the twins (its images cannot offer a login-gated scope)."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = render_blocks({})
        cls.verifier = cls.cfg["vc-verifier"]["common"]["credential_metadata"]
        cls.apigw = cls.cfg["vc-apigw"]["common"]["credential_metadata"]

    def test_no_twin_reaches_the_verifier(self):
        self.assertEqual([k for k in self.verifier if k.endswith("_offer")], [])

    def test_every_base_scope_stays_and_nym_auth_resolves_for_the_presets(self):
        base = {k for k in self.apigw if not k.endswith("_offer")}
        self.assertEqual(set(self.verifier), base)
        self.assertIn("nym_auth", self.verifier)
        self.assertEqual(self.verifier["nym_auth"]["format"], "mso_mdoc")
        self.assertEqual(self.verifier["nym_auth"]["mddl_file_path"], "/vctms/nym_auth.mdoc.json")

    def test_the_issuer_keeps_its_twins(self):
        twins = {k for k in self.apigw if k.endswith("_offer")}
        self.assertIn("nym_auth_offer", twins)
        self.assertTrue(all(t[:-len("_offer")] in self.verifier for t in twins), "every twin has its base scope")
        self.assertEqual(set(self.cfg["vc-issuer"]["common"]["credential_metadata"]) & twins, twins,
                         "issuer-core's block is the unfiltered one")

    def test_the_picker_sees_only_base_scopes(self):
        """The verifier's picker iterates common.credential_metadata wholesale; this is the count it renders."""
        twins = [k for k in self.apigw if k.endswith("_offer")]
        self.assertEqual(len(self.verifier), len(self.apigw) - len(twins))
        self.assertEqual(len(self.verifier), 14, "26 entries minus the 12 twins")


@NEEDS
class NoTwinsWhereOffersAreAllowed(unittest.TestCase):
    """issuer.allowPreauthOffer (SUNET/vc#840): the base scopes are offered directly, so the chart carries one entry per
    type everywhere - the issuer's credential_configurations_supported source included."""

    @classmethod
    def setUpClass(cls):
        values = env_config.load_environment_config("gdc").get("values") or {}
        cls.cfg = render_blocks(values)

    def test_no_twin_anywhere(self):
        for service, cfg in self.cfg.items():
            self.assertEqual([k for k in cfg["common"]["credential_metadata"] if k.endswith("_offer")], [], service)

    def test_one_entry_per_type_and_nym_auth_is_still_issuable(self):
        meta = self.cfg["vc-apigw"]["common"]["credential_metadata"]
        self.assertEqual(len(meta), 14)
        self.assertEqual(set(meta), set(self.cfg["vc-verifier"]["common"]["credential_metadata"]))
        scopes = self.cfg["vc-apigw"]["apigw"]["data_sources"]["datastore"]["scopes"]
        self.assertTrue(scopes["nym_auth"]["allow_preauth_offer"])
        self.assertEqual([k for k in scopes if k.endswith("_offer")], [])

    def test_the_twins_documents_are_not_imported(self):
        files = self.cfg["vc-apigw"]["apigw"]["data_sources"]["datastore"].get("import", {}).get("file_paths", [])
        self.assertEqual([f for f in files if "_offer" in f], [])


if __name__ == "__main__":
    unittest.main()
