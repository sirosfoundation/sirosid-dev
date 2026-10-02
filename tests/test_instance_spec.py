import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core.spec import InstanceSpec  # noqa: E402


class InstanceSpecTests(unittest.TestCase):
    def test_round_trips(self):
        s = InstanceSpec(env="x", region="arn", images={"pdp": "r/p:1"}, conformance=True,
                         trusted_issuers=["https://i"], values={"a": {"b": 1}}, bbs_secret_key="k")
        self.assertEqual(InstanceSpec.from_dict(s.to_dict()), s)

    def test_unknown_key_is_an_error_not_ignored(self):
        with self.assertRaises(ValueError) as cm:
            InstanceSpec.from_dict({"env": "x", "trusted_issuer": ["typo"]})
        self.assertIn("trusted_issuer", str(cm.exception))

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(ValueError):
            InstanceSpec.from_dict({"env": "x", "conformance": "yes"})
        with self.assertRaises(ValueError):
            InstanceSpec.from_dict({"env": "x", "trusted_issuers": "https://i"})

    def test_rical_needs_both_halves(self):
        with self.assertRaises(ValueError):
            InstanceSpec(env="x", rical_provider_url="https://r").validate()
        InstanceSpec(env="x", rical_provider_url="https://r", rical_root_pem="PEM").validate()

    def test_unknown_image_component_rejected_when_known_set_given(self):
        with self.assertRaises(ValueError):
            InstanceSpec(env="x", images={"nope": "i"}).validate(known_components=["pdp"])
        InstanceSpec(env="x", images={"pdp": "i"}).validate(known_components=["pdp"])

    def test_secrets_are_redacted_for_display(self):
        s = InstanceSpec(env="x", bbs_secret_key="topsecret")
        self.assertNotIn("topsecret", str(s.redacted()))
        self.assertEqual(s.to_dict()["bbs_secret_key"], "topsecret")

    def test_from_dict_copies_containers(self):
        src = {"env": "x", "trusted_issuers": ["a"]}
        s = InstanceSpec.from_dict(src)
        s.trusted_issuers.append("b")
        self.assertEqual(src["trusted_issuers"], ["a"])


if __name__ == "__main__":
    unittest.main()
