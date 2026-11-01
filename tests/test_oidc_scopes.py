#!/usr/bin/env python3
"""The apigw's OIDC provider asks for "openid profile" only. A credential type whose data is released under its own scope
(EHIC: mini-oidc's "ehic" scope) says so with issuance.oidcScopes, which the chart renders as vc's
oidc_request_params.extra_scopes on that assertion scope - and nowhere else."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")


def apigw_config(env_values=None):
    tmp = Path(tempfile.mkdtemp())
    try:
        render("fly", ROOT / "chart", env="t", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
               env_values=env_values or {}, naming=Naming(env="t"), say=lambda *a, **k: None, mongo_password="x")
        return yaml.safe_load((tmp / "o" / "fly-t" / "vc-apigw.yaml").read_text())["apigw"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class OidcScopes(unittest.TestCase):
    def test_ehic_asks_the_op_for_its_own_scope(self):
        scope = apigw_config()["data_sources"]["assertion"]["scopes"]["ehic"]
        self.assertEqual(scope["oidc_request_params"], {"extra_scopes": ["ehic"]})

    def test_a_scope_without_oidc_scopes_gets_no_request_params(self):
        scopes = apigw_config()["data_sources"]["assertion"]["scopes"]
        self.assertEqual([k for k, v in scopes.items() if k != "ehic" and "oidc_request_params" in v], [])

    def test_the_provider_scopes_stay_openid_profile(self):
        self.assertEqual(apigw_config()["auth_providers"]["oidc"]["scopes"], ["openid", "profile"])


if __name__ == "__main__":
    unittest.main()
