#!/usr/bin/env python3
"""verifier.extraVctms puts a VCTM in the vctms ConfigMap for a scope only the verifier has. vc loads a schema for every
non-mdoc scope at startup, so a verifier-only scope (set through verifier.extraConfig) without its file does not start."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")
DOC = {"vct": "urn:example:thing", "name": "Thing", "claims": [{"path": ["credentialSubject", "a"]}]}


def render_vctms(values):
    tmp = Path(tempfile.mkdtemp())
    try:
        render("fly", ROOT / "chart", env="t", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s",
               env_values=values, naming=Naming(env="t"), say=lambda *a, **k: None, mongo_password="x")
        d = tmp / "o" / "fly-t" / "vctms"
        return {p.name: p.read_text() for p in d.glob("*")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class ExtraVctms(unittest.TestCase):
    def test_an_object_is_written_as_json(self):
        files = render_vctms({"verifier": {"extraVctms": {"thing": DOC}}})
        self.assertEqual(json.loads(files["thing.json"]), DOC)

    def test_a_string_is_written_as_given(self):
        files = render_vctms({"verifier": {"extraVctms": {"thing": json.dumps(DOC)}}})
        self.assertEqual(json.loads(files["thing.json"]), DOC)

    def test_unset_adds_nothing(self):
        self.assertNotIn("thing.json", render_vctms({}))


if __name__ == "__main__":
    unittest.main()
