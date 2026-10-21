#!/usr/bin/env python3
"""gdc's PDP policies for context.verifier_ids (sirosfoundation/go-trust PR 218; PREPARED, deployed only with a go-trust
release that contains it). The point of the shape is what it does NOT change: policies exist only for the two verifier
actions, so every other action is evaluated by all registries exactly as before."""
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


@NEEDS
class GdcVerifierIds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp())
        try:
            values = env_config.load_environment_config("gdc").get("values") or {}
            render("fly", ROOT / "chart", env="gdc", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
                   env_values=values, naming=Naming(env="gdc"), say=lambda *a, **k: None, mongo_password="x")
            cls.cfg = yaml.safe_load((tmp / "o" / "fly-gdc" / "pdp.yaml").read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_only_the_two_verifier_actions_have_a_policy_and_each_returns_the_dns_name(self):
        policies = self.cfg["policies"]["policies"]
        self.assertEqual(set(policies), {"credential-verifier", "verifier"})
        for name, policy in policies.items():
            self.assertEqual(policy["constraints"], {"return_verifier_ids": ["x509_san_dns"]}, name)

    def test_nothing_else_is_constrained(self):
        """default_policy would route every unnamed action through a policy and fail_closed_on_unknown_action would deny
        every named action not listed; registries / allowed_key_types / require_key_binding would change who is asked."""
        section = self.cfg["policies"]
        for banned in ("default_policy", "fail_closed_on_unknown_action"):
            self.assertNotIn(banned, self.cfg, banned)
            self.assertNotIn(banned, section, banned)
        self.assertEqual(set(section), {"policies"})
        for policy in section["policies"].values():
            self.assertEqual(set(policy), {"description", "constraints"})
            self.assertEqual(set(policy["constraints"]), {"return_verifier_ids"})

    def test_the_whitelist_still_routes_the_verifier_actions(self):
        actions = self.cfg["registries"]["whitelist"]["actions"]
        self.assertEqual(actions["credential-verifier"], "verifiers")
        self.assertEqual(actions["verifier"], "verifiers")


if __name__ == "__main__":
    unittest.main()
