#!/usr/bin/env python3
"""The Accredify SG VCDM 2.0 presentation request is the DCQL Accredify's verifier uses: three ldp_vc queries with the
Singapore credential types, every requested credentialSubject claim mapped to an OIDC claim."""
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "vc-presentation-requests" / "accredify_sg_vcdm2.yaml"

VC = "https://www.w3.org/2018/credentials#VerifiableCredential"

TYPES = {
    "individualIdvc": [VC, "https://schemas.accredify.io/idvc/vocab/v1/vocab#IndividualIDCredential"],
    "crc": [VC, "https://schemas.accredify.io/idvc/vocab/v1/vocab#CorporateRepresentativeCredential"],
    "cec": [VC, "https://schemas.accredify.io/idvc/vocab/v1/vocab#CorporateEntityCredential"],
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

    def test_type_values_are_expanded_iris(self):
        """The verifier matches type_values as fully expanded IRIs (OpenID4VP 1.0 B.3.2) and rejects the
        whole query at /authorize when one is a bare name such as "VerifiableCredential"."""
        for query in self.template["dcql"]["credentials"]:
            for alternative in query["meta"]["type_values"]:
                for member in alternative:
                    self.assertRegex(member, r"^https://", query["id"])

    def test_every_requested_claim_is_a_credential_subject_claim_with_a_mapping(self):
        for query in self.template["dcql"]["credentials"]:
            for claim in query["claims"]:
                self.assertEqual(claim["path"][0], "credentialSubject")
                self.assertIn(claim["path"][1], self.template["claim_mappings"])

    def test_the_scope_names_the_template(self):
        self.assertEqual(self.template["oidc_scopes"], ["accredify_sg_vcdm2"])


if __name__ == "__main__":
    unittest.main()
