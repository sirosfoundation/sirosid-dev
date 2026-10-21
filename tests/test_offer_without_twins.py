#!/usr/bin/env python3
"""Offers without twins (SUNET/vc#840 `allow_preauth_offer`): the chart renders the flag only where an environment asks
for it, never on a preauth scope, and `make datastore-offer` follows the rendered config - the base scope when it is
flagged, the `<type>_offer` twin otherwise, and a named twin always."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import datastore  # noqa: E402
import env_config  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")


def render_scopes(env_values):
    tmp = Path(tempfile.mkdtemp())
    try:
        render("fly", ROOT / "chart", env="t", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
               env_values=env_values, naming=Naming(env="t"), say=lambda *a, **k: None, mongo_password="x")
        cfg = yaml.safe_load((tmp / "o" / "fly-t" / "vc-apigw.yaml").read_text())
        return cfg["apigw"]["data_sources"]["datastore"]["scopes"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class RenderedFlag(unittest.TestCase):
    def test_off_by_default_because_an_image_without_the_guard_ignores_it_and_one_with_it_may_refuse_to_start(self):
        scopes = render_scopes({})
        self.assertEqual([k for k, v in scopes.items() if "allow_preauth_offer" in v], [])

    def test_on_for_every_login_gated_scope_and_never_for_a_preauth_one(self):
        scopes = render_scopes({"issuer": {"allowPreauthOffer": True}})
        flagged = {k for k, v in scopes.items() if v.get("allow_preauth_offer")}
        gated = {k for k, v in scopes.items() if v["auth_provider"] != "preauth"}
        self.assertEqual(flagged, gated)
        self.assertIn("nym_auth", flagged)
        self.assertTrue(all("allow_preauth_offer" not in v for v in scopes.values() if v["auth_provider"] == "preauth"))

    def test_gdc_turns_it_on(self):
        values = env_config.load_environment_config("gdc").get("values") or {}
        self.assertTrue(values["issuer"]["allowPreauthOffer"])


class OfferScope(unittest.TestCase):
    SCOPES = {"nym_auth": {"auth_provider": "openid4vp", "allow_preauth_offer": True},
              "nym_auth_offer": {"auth_provider": "preauth"},
              "mdl": {"auth_provider": "oidc"}, "mdl_offer": {"auth_provider": "preauth"}}

    def test_a_flagged_base_scope_is_used_as_it_is(self):
        self.assertEqual(datastore.offer_scope(self.SCOPES, "nym_auth"), ("nym_auth", None))

    def test_an_unflagged_base_scope_falls_back_to_its_twin(self):
        self.assertEqual(datastore.offer_scope(self.SCOPES, "mdl"), ("mdl_offer", None))

    def test_a_named_twin_is_never_rewritten(self):
        self.assertEqual(datastore.offer_scope(self.SCOPES, "nym_auth_offer", "document_id_nym_auth_offer_alice-001"),
                         ("nym_auth_offer", "document_id_nym_auth_offer_alice-001"))

    def test_the_twin_style_document_id_names_the_base_document_against_a_flagged_scope(self):
        self.assertEqual(datastore.offer_scope(self.SCOPES, "nym_auth", "document_id_nym_auth_offer_alice-001"),
                         ("nym_auth", "document_id_nym_auth_alice-001"))
        self.assertEqual(datastore.offer_scope(self.SCOPES, "nym_auth", "document_id_nym_auth_alice-001"),
                         ("nym_auth", "document_id_nym_auth_alice-001"))

    def test_an_environment_without_the_flag_keeps_minting_against_the_twin(self):
        self.assertEqual(datastore.offer_scope({}, "nym_auth", "document_id_nym_auth_offer_alice-001"),
                         ("nym_auth_offer", "document_id_nym_auth_offer_alice-001"))


if __name__ == "__main__":
    unittest.main()
