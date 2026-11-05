#!/usr/bin/env python3
"""REGISTRY=external swaps each SD-JWT scope's vendored VCTM for its vct. A W3C (ldp_vc) scope has no vct and no registry
entry - the verifier-only Accredify scope names its DCQL type_values inline - so it must pass through untouched rather
than stop the render with "has no vct"."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core.vc_render import use_credential_registry  # noqa: E402

W3C = {"format": "ldp_vc", "credential_type_values": [["https://www.w3.org/2018/credentials#VerifiableCredential",
                                                         "https://example.org/vocab#Thing"]]}


def config():
    return {"common": {"credential_metadata": {
        "pid": {"format": "dc+sd-jwt", "vctm_file_path": "/x/pid.json"},
        "mdl": {"format": "mso_mdoc", "mddl_file_path": "/x/mdl.json"},
        "thing": dict(W3C),
    }}}


class W3CScopeSurvivesRegistryRender(unittest.TestCase):
    def test_w3c_scope_is_left_as_it_is(self):
        out = use_credential_registry(config(), {"pid": {"vct": "urn:pid:1"}}, ["https://registry.example"])
        self.assertEqual(out["common"]["credential_metadata"]["thing"], W3C)

    def test_sd_jwt_scope_still_resolves_through_its_vct(self):
        out = use_credential_registry(config(), {"pid": {"vct": "urn:pid:1"}}, ["https://registry.example"])
        pid = out["common"]["credential_metadata"]["pid"]
        self.assertEqual(pid["vct"], "urn:pid:1")
        self.assertNotIn("vctm_file_path", pid)

    def test_an_sd_jwt_scope_without_a_vct_still_stops_the_render(self):
        with self.assertRaises(SystemExit):
            use_credential_registry(config(), {}, ["https://registry.example"])


if __name__ == "__main__":
    unittest.main()
