"""Starting-point configs must always be valid, honest and safe to hand out."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sirosid_core import policy  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_core.templates import available_templates, config_templates, get_template  # noqa: E402


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.res = Resources(ROOT)

    def test_every_template_is_valid_for_a_user_holding_what_it_requires(self):
        for t in config_templates(self.res):
            problems = policy.validate(t.config, t.requires)
            self.assertEqual(problems, [], f"{t.id}: {[str(p) for p in problems]}")
            policy.build_spec(t.config, "tpl-check", t.requires)                         # and it becomes a real spec

    def test_templates_needing_a_capability_are_not_valid_without_it_and_are_not_offered(self):
        needing = [t for t in config_templates(self.res) if t.requires]
        self.assertTrue(needing, "there is a custom-image template")
        for t in needing:
            self.assertTrue(policy.validate(t.config, ()), f"{t.id} must be refused without {t.requires}")
        offered = {t["id"] for t in available_templates((), self.res)}
        self.assertFalse(offered & {t.id for t in needing})
        offered_all = {t["id"] for t in available_templates(policy.CAPABILITIES, self.res)}
        self.assertTrue({t.id for t in needing} <= offered_all)

    def test_ids_are_unique_and_names_are_safe_config_names(self):
        ids = [t.id for t in config_templates(self.res)]
        self.assertEqual(len(ids), len(set(ids)))
        for i in ids:
            self.assertRegex(i, r"^[a-z0-9][a-z0-9-]{0,62}$")
        self.assertEqual(ids[0], "standard", "the plain stack is offered first")

    def test_nothing_secret_private_or_personal_is_in_a_template(self):
        blob = json.dumps([t.to_dict() for t in config_templates(self.res)])
        for bad in ("fm2_", "FlyV1", "password", "secret", "BEGIN ", ".internal", "localhost", "127.0.0.1", "@"):
            self.assertNotIn(bad, blob, bad)

    def test_the_custom_image_template_starts_from_the_deployed_pin(self):
        t = get_template("custom-wallet-backend", policy.CAPABILITIES, self.res)
        pin = self.res.image_pin("walletBackend", "")
        self.assertTrue(pin)
        self.assertEqual(t["config"]["images"], {"wallet-backend": pin})

    def test_handing_out_a_template_cannot_change_it(self):
        a = available_templates(policy.CAPABILITIES, self.res)
        a[0]["config"]["injected"] = True
        self.assertNotIn("injected", available_templates(policy.CAPABILITIES, self.res)[0]["config"])
        self.assertIsNone(get_template("no-such-template", policy.CAPABILITIES, self.res))

    def test_every_template_says_what_it_is_for(self):
        for t in config_templates(self.res):
            self.assertGreater(len(t.title), 5)
            self.assertGreater(len(t.description), 60, t.id)


if __name__ == "__main__":
    unittest.main()
