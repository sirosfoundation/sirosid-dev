#!/usr/bin/env python3
"""The Accredify SG VCDM 2.0 presentation request is the DCQL Accredify's verifier uses: three ldp_vc queries with the
Singapore credential types, every requested credentialSubject claim mapped to an OIDC claim."""
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "vc-presentation-requests" / "accredify_sg_vcdm2.yaml"

TYPES = {
    "individualIdvc": ["VerifiableCredential", "IndividualIDCredential"],
    "crc": ["VerifiableCredential", "CorporateRepresentativeCredential"],
    "cec": ["VerifiableCredential", "CorporateEntityCredential"],
}


class AccredifyRequest(unittest.TestCase):
    def setUp(self):
        self.template = yaml.safe_load(FIXTURE.read_text())["templates"][0]

    def test_three_ldp_vc_queries_with_the_expected_types(self):
        queries = {q["id"]: q for q in self.template["dcql"]["credentials"]}
        self.assertEqual(set(queries), set(TYPES))
        for qid, types in TYPES.items():
            self.assertEqual(queries[qid]["format"], "ldp_vc")
            self.assertEqual(queries[qid]["meta"]["type_values"], [types])

    def test_every_requested_claim_is_a_credential_subject_claim_with_a_mapping(self):
        for query in self.template["dcql"]["credentials"]:
            for claim in query["claims"]:
                self.assertEqual(claim["path"][0], "credentialSubject")
                self.assertIn(claim["path"][1], self.template["claim_mappings"])

    def test_the_scope_names_the_template(self):
        self.assertEqual(self.template["oidc_scopes"], ["accredify_sg_vcdm2"])


if __name__ == "__main__":
    unittest.main()
