#!/usr/bin/env python3
"""Fly caps a machine's config body near 1 MiB, and every file we ship rides in it base64-encoded. gdc's apigw
hit 'http: request body too large' on 2026-10-20 when the pre-authorized twins copied the 282 KiB siros_id
VCTM and the documents. Pins how a twin ships and keeps each vc service's payload well under the cap."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core import vc_render  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")
CAP = 1024 * 1024
MARGIN = 0.85          # leave room for everything this estimate does not count (env, flags, JSON framing)


@NEEDS
class FlyPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        render("fly", ROOT / "chart", resources=Resources(ROOT), out_dir=cls.tmp / "out", secrets_dir=cls.tmp / "s",
               env_values={}, say=lambda *a, **k: None, mongo_password="x", env="payload")
        cls.out = cls.tmp / "out" / "fly-payload"
        cls.values = yaml.safe_load((ROOT / "values-base.yaml").read_text())["features"]["credentialTypes"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def twins(self):
        return {k: v for k, v in self.values.items() if v.get("metadataFrom")}

    def test_a_twin_reads_its_sources_metadata_and_ships_no_copy(self):
        shipped = {p.name for p in (self.out / "vctms").iterdir()}
        meta = yaml.safe_load((self.out / "vc-apigw.yaml").read_text())["common"]["credential_metadata"]
        self.assertTrue(self.twins())
        for twin, spec in self.twins().items():
            src = spec["metadataFrom"]
            self.assertIn(src, self.values, twin)
            for k in ("vct", "doctype", "format", "vctm", "mdocSchema"):
                self.assertEqual(spec.get(k), self.values[src].get(k), f"{twin}.{k}")
            suffix = ".mdoc.json" if spec.get("mdocSchema") else ".json"
            self.assertNotIn(twin + suffix, shipped, f"{twin} ships its own copy of {src}'s metadata")
            path = meta[twin].get("mddl_file_path") or meta[twin].get("vctm_file_path")
            self.assertEqual(path, f"/vctms/{src}{suffix}", twin)
            self.assertIn(src + suffix, shipped)

    def test_twin_documents_are_not_imported_or_shipped_on_fly(self):
        imports = yaml.safe_load((self.out / "vc-apigw.yaml").read_text())["apigw"]["data_sources"]["datastore"]["import"]["file_paths"]
        self.assertTrue(imports)
        self.assertFalse([p for p in imports if vc_render.is_twin_document(p)], "an import path with no file crashes the apigw")

    def test_every_vc_service_stays_well_under_the_cap(self):
        def size(*paths):
            total = 0
            for path in paths:
                total += sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size
            return total * 4 // 3                                   # base64
        docs = [f for f in (self.out / "documents").iterdir() if not vc_render.is_twin_document(f.name)]
        common = [self.out / "branding-assets", self.out / "vctms"]
        for service, extra in {"vc-apigw": docs, "vc-issuer": [], "vc-registry": [], "vc-verifier": [self.out / "pres-reqs"]}.items():
            body = size(self.out / f"{service}.yaml", *common, *extra)
            self.assertLess(body, CAP * MARGIN, f"{service} would ship ~{body // 1024} KiB of a {CAP // 1024} KiB cap")


if __name__ == "__main__":
    unittest.main()
