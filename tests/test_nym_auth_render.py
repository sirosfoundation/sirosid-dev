#!/usr/bin/env python3
"""nym_auth, as the chart renders it for vc: an openid4vp-authenticated
datastore scope accepting any one of five presented credentials (SD-JWT PIDs
and mdocs), with the claim names vc needs for each, offered by every client a
wallet uses, and verifiable/requestable everywhere the other types are.

Settled against SUNET/vc main (internal/apigw/apiv1/handlers_verifier.go
buildIssuanceAuthDCQL/authClaimPathSegments, handlers_issuer.go
ResolveIdentifier, pkg/configuration checkAuthScopes) - see values-base.yaml's
nym_auth comment for why each name is what it is.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sirosid_core.render import render  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402


NEEDS = unittest.skipUnless(shutil.which("helm"), "needs helm")

SCOPE = "nym_auth"
DOCTYPE = "eu.europa.ec.eudi.nym_auth.1"

# auth scope -> (auth_claims, identity_attributes, document_claims)
EXPECTED_AUTH_SCOPES = {
    # The SD-JWT PIDs also copy the national identifier and issuing country: the seed's subjects follow the presented PID.
    "pid_1_8": (["given_name", "family_name", "birthdate"], {"birthdate": "birth_date"},
                {"birthdate": "birth_date", "personal_administrative_number": "personal_administrative_number",
                 "issuing_country": "issuing_country"}),
    "pid_1_5": (["given_name", "family_name", "birthdate"], {"birthdate": "birth_date"},
                {"birthdate": "birth_date", "personal_administrative_number": "personal_administrative_number",
                 "issuing_country": "issuing_country"}),
    "pid_mdoc": (["eu.europa.ec.eudi.pid.1.given_name", "eu.europa.ec.eudi.pid.1.family_name"],
                 {"eu.europa.ec.eudi.pid.1.given_name": "given_name",
                  "eu.europa.ec.eudi.pid.1.family_name": "family_name"},
                 {"eu.europa.ec.eudi.pid.1.birth_date": "birth_date"}),
    "mdl": (["given_name", "family_name"], None, {"birth_date": "birth_date"}),
    "photoid": (["org.iso.23220.1.given_name_unicode", "org.iso.23220.1.family_name_unicode"],
                {"org.iso.23220.1.given_name_unicode": "given_name",
                 "org.iso.23220.1.family_name_unicode": "family_name"},
                {"org.iso.23220.1.birth_date": "birth_date"}),
}
MDOC_AUTH_SCOPES = {"pid_mdoc", "mdl", "photoid"}


def render_target(target):
    """{service: parsed config} plus the rendered directory's file contents."""
    tmp = Path(tempfile.mkdtemp())
    try:
        kwargs = {"env": "nymtest"} if target == "fly" else {}
        render(target, ROOT / "chart", resources=Resources(ROOT), out_dir=tmp / "out", secrets_dir=tmp / "secrets",
               env_values={}, say=lambda *a, **k: None, mongo_password="x", **kwargs)
        base = tmp / "out" / "fly-nymtest" if target == "fly" else tmp / "out"
        configs = {svc: yaml.safe_load((base / f"{svc}.yaml").read_text())
                   for svc in ("vc-apigw", "vc-issuer", "vc-verifier")}
        files = {
            "mddl": (base / "vctms" / f"{SCOPE}.mdoc.json").read_bytes(),
            "documents": json.loads((base / "documents" / f"{SCOPE}.json").read_text()),
        }
        return configs, files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class NymAuthRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {t: render_target(t) for t in ("compose", "fly")}

    def each(self):
        for target, (configs, files) in self.rendered.items():
            with self.subTest(target=target):
                yield target, configs, files

    def test_datastore_scope_is_openid4vp_with_the_five_auth_scopes(self):
        for _, configs, _ in self.each():
            scope = configs["vc-apigw"]["apigw"]["data_sources"]["datastore"]["scopes"][SCOPE]
            self.assertEqual(scope["auth_provider"], "openid4vp")
            self.assertEqual(scope["authentic_source"], "mini-oidc")
            self.assertNotIn("auth_claims", scope, "vc refuses auth_claims on an openid4vp scope")
            self.assertEqual(set(scope["auth_scopes"]), set(EXPECTED_AUTH_SCOPES))
            for name, (claims, identity, document) in EXPECTED_AUTH_SCOPES.items():
                entry = scope["auth_scopes"][name]
                self.assertEqual(entry["auth_claims"], claims, name)
                self.assertEqual(entry.get("identity_attributes"), identity, name)
                self.assertEqual(entry.get("document_claims"), document, name)

    def test_no_mdoc_auth_claim_is_a_date(self):
        # vc main: an mdoc full-date reaches identity extraction as CBOR tag
        # 1004, which ExtractIdentityClaims refuses ("not string") - that would
        # fail every presentation of that credential, not just this claim.
        for _, configs, _ in self.each():
            scopes = configs["vc-apigw"]["apigw"]["data_sources"]["datastore"]["scopes"][SCOPE]["auth_scopes"]
            for name in MDOC_AUTH_SCOPES:
                for claim in scopes[name]["auth_claims"]:
                    self.assertNotIn("date", claim, name)

    def test_every_auth_scope_names_configured_credential_metadata(self):
        # pkg/configuration checkAuthScopes, and what buildIssuanceAuthDCQL
        # takes the format and doctype/vct constraint from.
        for _, configs, _ in self.each():
            for svc in ("vc-apigw", "vc-issuer"):
                meta = configs[svc]["common"]["credential_metadata"]
                for name in EXPECTED_AUTH_SCOPES:
                    self.assertIn(name, meta, f"{svc}: {name}")
                for name in MDOC_AUTH_SCOPES:
                    self.assertEqual(meta[name]["format"], "mso_mdoc", name)
                    self.assertTrue(meta[name].get("doctype"), name)

    def test_credential_metadata_and_documents(self):
        for _, configs, files in self.each():
            for svc in ("vc-apigw", "vc-issuer"):
                meta = dict(configs[svc]["common"]["credential_metadata"][SCOPE])
                # No auxiliary_data in the default render (it needs a vc image with #850);
                # test_aux_data_render holds the gdc-style render.
                self.assertNotIn("auxiliary_data", meta, svc)
                self.assertEqual(meta, {"mddl_file_path": f"/vctms/{SCOPE}.mdoc.json", "doctype": DOCTYPE,
                                        "format": "mso_mdoc"}, svc)
            self.assertEqual(files["mddl"], (ROOT / "fixtures" / "vc-metadata" / f"{SCOPE}.mdoc.json").read_bytes())
            # the documents are the source fixtures: no ssc or psc, vc derives and the wallet supplies them
            expected = json.loads((ROOT / "fixtures" / "vc-bootstrapping" / f"{SCOPE}.json").read_text())
            self.assertEqual(files["documents"], expected)
            imports = configs["vc-apigw"]["apigw"]["data_sources"]["datastore"]["import"]["file_paths"]
            self.assertIn(f"/documents/{SCOPE}.json", imports)

    def test_every_wallet_client_may_request_it_and_the_mdoc_auth_credentials(self):
        for target, configs, _ in self.each():
            clients = configs["vc-apigw"]["apigw"]["delivery"]["openid4vci"]["clients"]
            self.assertIn("e2e-test-client", clients)
            if target == "compose":
                self.assertIn("e2e-test-client-2", clients)
            for client_id, client in clients.items():
                for scope in (SCOPE, "pid_1_8", "pid_1_5", "pid_mdoc", "mdl", "photoid"):
                    self.assertIn(scope, client["scopes"], f"{target}: {client_id} lacks {scope}")

    def test_verifier_supports_it(self):
        for _, configs, _ in self.each():
            openid4vp = configs["vc-verifier"]["verifier"]["inbound"]["openid4vp"]
            supported = {s for c in openid4vp["supported_credentials"] for s in c["scopes"]
                         if c["vct"] == DOCTYPE}
            self.assertEqual(supported, {SCOPE})
            for client_id, client in openid4vp["clients"].items():
                self.assertIn(SCOPE, client["scopes"], client_id)


if __name__ == "__main__":
    unittest.main()
