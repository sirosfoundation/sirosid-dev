#!/usr/bin/env python3
"""The Accredify SG VCDM 2.0 presentation requests: one ldp_vc query per Singapore credential type (the verifier cannot
attribute several queries to scopes without credential_metadata), every requested credentialSubject claim mapped to an
OIDC claim."""
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "vc-presentation-requests" / "accredify_sg_vcdm2.yaml"

VC = "https://www.w3.org/2018/credentials#VerifiableCredential"
VOCAB = "https://schemas.accredify.io/idvc/vocab/v1/vocab#"

# scope -> (query id, credential type)
SCOPES = {
    "accredify_individual_id": ("individualIdvc", "IndividualIDCredential"),
    "accredify_corporate_representative": ("crc", "CorporateRepresentativeCredential"),
    "accredify_corporate_entity": ("cec", "CorporateEntityCredential"),
}


class AccredifyRequests(unittest.TestCase):
    def setUp(self):
        self.templates = {t["oidc_scopes"][0]: t for t in yaml.safe_load(FIXTURE.read_text())["templates"]}

    def test_one_template_per_scope_each_with_a_single_ldp_vc_query(self):
        self.assertEqual(set(self.templates), set(SCOPES))
        for scope, (qid, credential_type) in SCOPES.items():
            template = self.templates[scope]
            self.assertEqual(template["oidc_scopes"], [scope])
            (query,) = template["dcql"]["credentials"]
            self.assertEqual(query["id"], qid)
            self.assertEqual(query["format"], "ldp_vc")
            self.assertEqual(query["meta"]["type_values"], [[VC, VOCAB + credential_type]])

    def test_type_values_are_expanded_iris(self):
        """The verifier matches type_values as fully expanded IRIs (OpenID4VP 1.0 B.3.2) and rejects the
        whole query at /authorize when one is a bare name such as "VerifiableCredential"."""
        for template in self.templates.values():
            for query in template["dcql"]["credentials"]:
                for alternative in query["meta"]["type_values"]:
                    for member in alternative:
                        self.assertRegex(member, r"^https://", query["id"])

    def test_every_template_has_exactly_one_query(self):
        """With several queries the verifier needs a credential_metadata scope per query and answers
        server_error at /authorize without them."""
        for template in self.templates.values():
            self.assertEqual(len(template["dcql"]["credentials"]), 1, template["id"])

    def test_every_requested_claim_is_a_credential_subject_claim_with_a_mapping(self):
        for template in self.templates.values():
            for query in template["dcql"]["credentials"]:
                for claim in query["claims"]:
                    self.assertEqual(claim["path"][0], "credentialSubject")
                    self.assertIn(claim["path"][1], template["claim_mappings"])


if __name__ == "__main__":
    unittest.main()
