#!/usr/bin/env python3
"""nym_auth's seed follows the PRESENTED PID (SUNET/vc#838 + #793): the SD-JWT PID scopes copy the PID's national identifier and issuing
country into the issued document, replacing what the datastore stores, and the ssc commitment reads exactly those two elements.

What this proves is the configuration; that vc really derives the seed from the presentation is vc's behaviour (presented_claims.go:
applyPresentedDocumentClaims REPLACES the stored elements, injectCommitments runs on the decorated document)."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import env_config  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")
SUBJECTS = ["issuing_country", "personal_administrative_number"]


def vctm_claims(name):
    d = json.load(open(ROOT / "fixtures" / "vc-metadata" / name))
    return {tuple(c.get("path", []))[0] for c in d.get("claims", []) if c.get("path")}


@NEEDS
class SeedFromPid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        values = env_config.load_environment_config("gdc")["values"]
        tmp = Path(tempfile.mkdtemp())
        try:
            render("fly", ROOT / "chart", env="t", resources=Resources(ROOT), out_dir=tmp / "o", secrets_dir=tmp / "s", env_values=values,
                   naming=Naming(env="t"), say=lambda *a, **k: None, mongo_password="x")
            cfg = yaml.safe_load((tmp / "o" / "fly-t" / "vc-apigw.yaml").read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        cls.scope = cfg["apigw"]["data_sources"]["datastore"]["scopes"]["nym_auth"]
        cls.meta = cfg["common"]["credential_metadata"]["nym_auth"]

    def test_the_opening_reads_the_two_identifiers_of_the_issued_document(self):
        self.assertEqual(self.scope["derivations"][0]["hash_commitment"]["opening"]["hkdf"]["inputs"], SUBJECTS)

    def test_the_sd_jwt_pid_scopes_copy_both_identifiers_from_the_presentation(self):
        for pid, vctm in (("pid_1_8", "vctm_pid_arf_1_8.json"), ("pid_1_5", "vctm_pid_arf_1_5.json")):
            entry = self.scope["auth_scopes"][pid]
            for subject in SUBJECTS:
                self.assertEqual(entry["document_claims"].get(subject), subject, f"{pid}: {subject} is not copied from the PID")
                self.assertIn(subject, vctm_claims(vctm), f"{pid}: the PID's VCTM does not declare {subject}")

    def test_the_issuer_asks_the_wallet_for_them_too_the_document_claim_sources_join_the_request(self):
        """vc adds every document_claims source to the DCQL it sends the wallet (handlers_verifier.go), so no auth_claims entry is needed."""
        entry = self.scope["auth_scopes"]["pid_1_8"]
        self.assertNotIn("personal_administrative_number", entry["auth_claims"])
        self.assertIn("personal_administrative_number", entry["document_claims"])

    def test_scopes_that_cannot_supply_them_do_not_pretend_to(self):
        for other in ("mdl", "photoid", "pid_mdoc"):
            self.assertFalse(set(SUBJECTS) & set(self.scope["auth_scopes"][other].get("document_claims", {}).values()), other)

    def test_the_stored_documents_still_carry_the_same_identifiers_so_the_pseudonym_is_the_same_by_offer_or_by_pid(self):
        pid = json.load(open(ROOT / "fixtures" / "vc-bootstrapping" / "pid_1_8.json"))
        nym = json.load(open(ROOT / "fixtures" / "vc-bootstrapping" / "nym_auth.json"))

        def one(d, person):
            v = d[person]
            return (v[0] if isinstance(v, list) else v)["document_data"]
        for person in ("alice-001", "bob-002"):
            for subject in SUBJECTS:
                self.assertEqual(one(nym, person)[subject], one(pid, person)[subject], f"{person} {subject}")


if __name__ == "__main__":
    unittest.main()
