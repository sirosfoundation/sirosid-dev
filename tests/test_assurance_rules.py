"""The gdc/nymauth engine gets its assurance policy: a ConfigMap from the chart, the rules file uploaded to the path
the engine config names, and the soft-claim switches off (go-wallet-backend refuses to start without the file)."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "chart" / "config" / "assurance-gdc.rules"


class AssuranceRules(unittest.TestCase):
    def values(self, env):
        return yaml.safe_load((ROOT / "environments" / f"{env}.yaml").read_text())["values"]["walletBackend"]

    def test_rules_file_has_the_four_rules(self):
        rules = [l for l in RULES.read_text().splitlines() if l.startswith("(assure-")]
        self.assertEqual(len(rules), 4)

    def test_environments_configure_it_the_same_way(self):
        for env in ("gdc", "nymauth"):
            wb = self.values(env)
            self.assertEqual(wb["assuranceRules"], "config/assurance-gdc.rules", env)
            a = wb["extraConfig"]["wallet_provider"]["attestation"]["assurance"]
            self.assertTrue(a["enabled"], env)
            self.assertEqual(a["rules_file"], "/etc/wallet/assurance/assurance.rules", env)
            # the defaults are true; true would make the result min(claimed, granted) and defeat the simulation
            self.assertIs(a["lower_by_claims_key_storage"], False, env)
            self.assertIs(a["lower_by_claims_user_authentication"], False, env)

    def test_both_targets_upload_to_the_configured_path(self):
        for f in ("sirosid_core/deploy.py", "sirosid_core/singlemachine.py"):
            self.assertIn("/etc/wallet/assurance/", (ROOT / f).read_text(), f)

    def test_the_chart_default_renders_no_configmap(self):
        self.assertEqual(yaml.safe_load((ROOT / "chart" / "values.yaml").read_text())["walletBackend"]["assuranceRules"], "")


if __name__ == "__main__":
    unittest.main()
