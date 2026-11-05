#!/usr/bin/env python3
"""The gdc verifier's own page offers the Accredify Individual ID request as a button.

A preset is keyed by a credential_metadata scope, and for an ldp_vc credential that scope is where the DCQL
meta.type_values come from (expanded IRIs; a verifier refuses a W3C scope without a narrowing type). The scope lives
in the verifier's config only: the issuer shares common.credential_metadata, and an issuer given a W3C scope will not
start without a JSON-LD context allowlist."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_nymauth_verifier_render import NEEDS, render_gdc_verifier  # noqa: E402

SCOPE = "accredify_individual_id"
LABEL = "Accredify SG - Individual ID (VCDM 2.0)"
TYPES = [["https://www.w3.org/2018/credentials#VerifiableCredential",
          "https://schemas.accredify.io/idvc/vocab/v1/vocab#IndividualIDCredential"]]


@NEEDS
class AccredifyButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.cfg = render_gdc_verifier()

    def test_scope_asks_for_the_expanded_individual_id_type(self):
        meta = self.cfg["common"]["credential_metadata"][SCOPE]
        self.assertEqual(meta["format"], "ldp_vc")
        self.assertEqual(meta["credential_type_values"], TYPES)

    def test_scope_names_the_type_and_context_vc_requires_at_startup(self):
        """vc's checkW3CTypeConsistency panics the verifier at startup when a W3C scope narrows the request (type_values
        past VerifiableCredential) without naming the type it issues and a context that defines it. Seen live: the
        verifier crash-looped on the first version of this scope."""
        meta = self.cfg["common"]["credential_metadata"][SCOPE]
        self.assertIn("IndividualIDCredential", meta["credential_types"])
        self.assertEqual(meta["credential_contexts"], ["https://schemas.accredify.io/idvc/individual-id/v1/context"])

    def test_preset_requests_the_claims_the_oidc_template_maps(self):
        preset = self.cfg["verifier"]["presets"][LABEL]
        self.assertEqual(preset["category"], "Accredify")
        claims = preset["credentials"][SCOPE]["claims"]
        self.assertEqual([c["path"] for c in claims],
                         [["credentialSubject", "fullName"], ["credentialSubject", "email"]])

    def test_the_other_presets_and_scopes_are_still_there(self):
        self.assertIn("nym_auth", self.cfg["common"]["credential_metadata"])
        self.assertIn("nym_auth_pseudonym", self.cfg["verifier"]["presets"])


if __name__ == "__main__":
    unittest.main()
