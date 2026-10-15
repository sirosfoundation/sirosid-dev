#!/usr/bin/env python3
"""vc builds with SUNET/vc#507 + #735 refuse an OIDC/openid4vp login with no identity-mapping namespace
(IDENTITY_MAPPING_NAMESPACE_REQUIRED): every issuance in a gdc running upstream main failed with it. The chart must
render `authentic_source` on every datastore scope that needs it - by default, for every environment."""
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


def apigw_scopes(env_values=None):
    tmp = Path(tempfile.mkdtemp())
    try:
        render("compose", ROOT / "chart", resources=Resources(ROOT), out_dir=tmp / "out", secrets_dir=tmp / "secrets",
               env_values=env_values or {}, say=lambda *a, **k: None, mongo_password="x")
        cfg = yaml.safe_load((tmp / "out" / "vc-apigw.yaml").read_text())
        return cfg["apigw"]["data_sources"]["datastore"]["scopes"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class DatastoreAuthenticSource(unittest.TestCase):
    def test_every_non_preauth_datastore_scope_carries_the_namespace_by_default(self):
        scopes = apigw_scopes()
        self.assertTrue(scopes)
        for name, sc in scopes.items():
            if sc.get("auth_provider") == "preauth":
                continue                                    # see test_preauth_scopes_never_get_one
            self.assertEqual(sc.get("authentic_source"), "mini-oidc", f"{name} ({sc.get('auth_provider')})")

    def test_the_namespace_is_the_one_the_fixture_mappings_are_imported_under(self):
        import json
        mappings = json.loads((ROOT / "fixtures" / "vc-bootstrapping" / "identity_mappings.json").read_text())
        namespaces = {m["authentic_source"] for person in mappings.values() for m in person}
        self.assertEqual(namespaces, {"mini-oidc"}, "the chart default and the imported mappings must agree")

    def test_it_can_be_overridden_and_switched_off_for_older_vc(self):
        for name, sc in apigw_scopes({"issuer": {"datastoreAuthenticSource": "SUNET"}}).items():
            if sc.get("auth_provider") != "preauth":
                self.assertEqual(sc["authentic_source"], "SUNET", name)
        for name, sc in apigw_scopes({"issuer": {"datastoreAuthenticSource": ""}}).items():
            self.assertNotIn("authentic_source", sc, name)

    def test_preauth_scopes_never_get_one(self):
        # preauth carries the identifier in the offer; vc documents authentic_source as unused there
        tmpl = (ROOT / "chart" / "templates" / "04-issuer.yaml").read_text()
        self.assertIn('ne $data.issuance.authProvider "preauth"', tmpl)
        preauth = [n for n, sc in apigw_scopes().items() if sc.get("auth_provider") == "preauth"]
        self.assertTrue(preauth, "the chart declares pre-authorized scopes")
        for name in preauth:
            self.assertNotIn("authentic_source", apigw_scopes()[name], name)


if __name__ == "__main__":
    unittest.main()
