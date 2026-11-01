import re
import tomllib
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent


class SirosVerifierConfigTests(unittest.TestCase):
    def ui_config(self, filename):
        text = (ROOT / "fixtures" / "siros-verifier" / filename).read_text()
        # The existing DCQL uses TOML 1.1 multiline inline tables, which
        # Python's TOML 1.0 parser does not accept. Parse the new UI table
        # itself and verify that it occurs exactly once in the UI frontend.
        match = re.search(
            r"(?ms)^type = \"ui\"\nname = \"ui\"\n"
            r"(?:  )?\[frontend\.config\]\n(.*?)"
            r"(?=^  \[\[frontend\.config\.presets\]\]|^\[\[frontend\]\])",
            text,
        )
        self.assertIsNotNone(match)
        return tomllib.loads(match.group(1))

    def test_proxy_fixtures_have_identical_registry_config(self):
        local = self.ui_config("proxy.toml")
        fly = self.ui_config("proxy.fly.toml")
        self.assertEqual(local, fly)
        self.assertEqual(
            set(local),
            {
                "invocations",
                "schemes",
                "registry_url",
                "registry_allowed_hosts",
                "registry_cache_seconds",
            },
        )
        registry_url = urlsplit(local["registry_url"])
        self.assertEqual(registry_url.scheme, "https")
        self.assertIn(registry_url.hostname, local["registry_allowed_hosts"])

    def storage_config(self, filename):
        text = (ROOT / "fixtures" / "siros-verifier" / filename).read_text()
        matches = list(
            re.finditer(
                r"(?ms)^  \[backend\.config\.storage\]\n(.*?)(?=^  \[|\Z)",
                text,
            )
        )
        self.assertEqual(len(matches), 1, "exactly one backend storage table")
        return tomllib.loads(matches[0].group(1))

    def test_proxy_fixtures_explicitly_use_shared_mongo_storage(self):
        local = self.storage_config("proxy.toml")
        fly = self.storage_config("proxy.fly.toml")
        for config in (local, fly):
            self.assertEqual(set(config), {"backend", "uri", "database", "collection"})
            self.assertEqual(config["backend"], "mongodb")
            self.assertEqual(config["database"], "siros_verifier")
            self.assertEqual(config["collection"], "presentation_flows")
        self.assertEqual(local["uri"], "mongodb://mongodb:27017")
        self.assertEqual(fly["uri"], "${SIROS_VERIFIER_MONGODB_URI}")

    def zkp_circuit_config(self, filename):
        text = (ROOT / "fixtures" / "siros-verifier" / filename).read_text()
        match = re.search(
            r"(?ms)^  \[backend\.config\.zkp\.circuits\]\n(.*)\Z",
            text,
        )
        self.assertIsNotNone(match)
        parsed = tomllib.loads(match.group(1))
        return {
            "sources": parsed["sources"],
            "allow": parsed["backend"]["config"]["zkp"]["circuits"]["allow"],
        }

    def test_proxy_fixtures_use_the_pinned_siros_catalog_circuit(self):
        local = self.zkp_circuit_config("proxy.toml")
        fly = self.zkp_circuit_config("proxy.fly.toml")
        self.assertEqual(local, fly)
        self.assertEqual(local["sources"], ["https://api.circuits.siros.org"])
        self.assertEqual(len(local["allow"]), 1)
        circuit = local["allow"][0]
        self.assertEqual(circuit["artifact_id"], "nymauth-longfellow-mdoc-v1")
        self.assertNotIn("artifact_path", circuit)
        self.assertEqual(circuit["artifact_bytes"], 1062842)
        self.assertEqual(
            circuit["artifact_sha256"],
            "92dd42fdf99673249eec33c7d07850bd9da66fbdab01ef26391e04c8341c1b7f",
        )

    def test_gdc_san_and_hash_verifier_trust_are_pinned(self):
        config = yaml.safe_load((ROOT / "environments" / "gdc.yaml").read_text())
        self.assertIn(
            "https://sirosid-gdc-siros-verifier.fly.dev",
            config["trusted_verifiers"],
        )
        self.assertIn(
            "x509_hash:cIPUuz0YSWx4RxTKJXF3VCCh2oWL3HD2Knjjgd_vKRo",
            config["trusted_verifiers"],
        )
        root = "fixtures/trusted-roots/sirosid-gdc-test-ca.pem"
        self.assertIn(root, config["trusted_verifier_roots"])
        self.assertTrue((ROOT / root).is_file())

    def test_gdc_pin_has_a_local_epoch_tag_and_digest_provenance(self):
        text = (ROOT / "environments" / "gdc.yaml").read_text()
        image = yaml.safe_load(text)["images"]["siros-verifier"]
        self.assertRegex(
            image,
            r"^registry\.fly\.io/sirosid-gdc-siros-verifier:local-[1-9][0-9]*$",
        )
        self.assertRegex(
            text,
            rf"# Image digest sha256:[0-9a-f]{{64}}\.\n"
            rf"\s+# Build: .*\n\s+siros-verifier: {re.escape(image)}\n",
        )


if __name__ == "__main__":
    unittest.main()
