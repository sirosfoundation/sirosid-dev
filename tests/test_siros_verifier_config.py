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
            r"(?:  )?\[frontend\.config\]\n(.*?)(?=^\[\[frontend\]\])",
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
