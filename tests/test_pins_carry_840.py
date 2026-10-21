#!/usr/bin/env python3
"""Standing rule (until SUNET/vc#840 is merged to vc main): an environment that sets issuer.allowPreauthOffer must pin vc
images built from a vc SHA that contains #840. An image without it IGNORES the flag silently, so a pre-authorized offer for
a base scope would fail with invalid_scope and nothing in the config says why. The pins' provenance (environments/gdc.yaml's
PIN PROVENANCE block) must therefore name #840, and every environment that sets the flag must pin the same vc images as the
environment that records them."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import env_config  # noqa: E402

VC = ("vc-apigw", "vc-issuer", "vc-registry", "vc-verifier")


class PinsCarry840(unittest.TestCase):
    def flagged(self):
        out = []
        for path in sorted((ROOT / "environments").glob("*.yaml")):
            cfg = env_config.load_environment_config(path.stem)
            if ((cfg.get("values") or {}).get("issuer") or {}).get("allowPreauthOffer"):
                out.append((path.stem, cfg))
        return out

    def test_gdc_is_one_of_them(self):
        self.assertIn("gdc", [n for n, _ in self.flagged()])

    def test_the_provenance_block_names_840_and_the_sha_it_was_built_from(self):
        text = (ROOT / "environments" / "gdc.yaml").read_text()
        block = text[text.index("PIN PROVENANCE"):text.index("# Other images are released versions")]
        self.assertIn("https://github.com/SUNET/vc/pull/840", block)
        self.assertRegex(block, r"integration/gdc-nymauth-v5` @ [0-9a-f]{40}")

    def test_flagged_environments_pin_the_same_vc_images_as_gdc(self):
        gdc = env_config.load_environment_config("gdc")["images"]
        for name, cfg in self.flagged():
            for component in VC:
                self.assertEqual(cfg["images"][component], gdc[component], f"{name}: {component}")
                self.assertRegex(cfg["images"][component], r":local-\d+$|:\d", f"{name}: {component} is not a pinned build")

    def test_one_epoch_tag_for_all_four_vc_images(self):
        """They are built together from one SHA and share one config shape: mixing builds crash-loops the older one."""
        gdc = env_config.load_environment_config("gdc")["images"]
        self.assertEqual(len({gdc[c].rsplit(":", 1)[1] for c in VC}), 1)


if __name__ == "__main__":
    unittest.main()
