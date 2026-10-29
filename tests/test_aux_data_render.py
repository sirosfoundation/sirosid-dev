#!/usr/bin/env python3
"""nym_auth's Credential Auxiliary Data (SUNET/vc#850): the chart must give the issuer-apigw - and only it - the type's
auxiliary_data, one hash_commitment derivation on every data source scope serving it, and a usable secret file for the
opening. An image without #850 ignores all of it silently and issues a credential with no ssc, and one that is not
given the removed keys' deletion would also start cleanly with no commitment, so what we render is the only line of
defence."""
import base64
import json
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


# What environments/gdc.yaml switches on (the default is off: it needs a vc image with SUNET/vc#850).
GDC_VALUES = {"issuer": {"apigw": {"credentialEncryption": {"enabled": True}}}}
SEED_PATH = "/seeds/nym_auth_seed.key"
SSC = ["eu.europa.ec.eudi.nym_auth.1", "ssc"]
PSC = ["eu.europa.ec.eudi.nym_auth.1", "psc"]


def rendered(secrets_dir=None, env_values=None, documents=False):
    tmp = Path(tempfile.mkdtemp())
    out = {}
    try:
        render("compose", ROOT / "chart", resources=Resources(ROOT), out_dir=tmp / "out",
               secrets_dir=secrets_dir or tmp / "secrets", env_values=env_values or {}, say=lambda *a, **k: None,
               mongo_password="x")
        for f in ("vc-apigw", "vc-issuer", "vc-verifier", "vc-registry"):
            out[f] = yaml.safe_load((tmp / "out" / f"{f}.yaml").read_text())
        if documents:
            out["documents"] = {p.name: json.loads(p.read_text()) for p in (tmp / "out" / "documents").glob("nym_auth*.json")}
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@NEEDS
class AuxiliaryData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = rendered(cls.tmp / "secrets", env_values=GDC_VALUES)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_seeds_block_is_rendered_any_more(self):
        """vc decodes config non-strictly: a leftover `seeds` starts cleanly, is ignored, and credentials issue with
        no commitment at all, so the chart must not render it."""
        self.assertNotIn("seeds", self.cfg["vc-apigw"]["apigw"])

    def test_the_opening_secret_is_a_file_not_config(self):
        paths = vc_render.opening_secret_paths(self.cfg["vc-apigw"]["apigw"])
        self.assertEqual(paths, [SEED_PATH])
        raw = base64.urlsafe_b64decode((self.tmp / "secrets" / vc_render.opening_secret_file(SEED_PATH)).read_text())
        self.assertGreaterEqual(len(raw), 32, "vc refuses an opening secret under 32 bytes decoded")
        self.assertNotIn("secret:", yaml.dump(self.cfg["vc-apigw"]["apigw"]["data_sources"]), "the secret never sits in the config")

    def test_every_opening_secret_can_be_mounted_by_the_single_machine_layout(self):
        from sirosid_core import singlemachine
        for path in vc_render.opening_secret_paths(self.cfg["vc-apigw"]["apigw"]):
            self.assertIn(path, singlemachine.SECRET_PATHS["vc"], f"the single-machine layout cannot mount {path}")
        self.assertIn(f"vc-secrets/{vc_render.opening_secret_file(SEED_PATH)}", [s for s, _ in singlemachine.SECRETS.values()])

    def test_nym_auth_and_its_twin_carry_the_same_auxiliary_data_and_derivation(self):
        meta = self.cfg["vc-apigw"]["common"]["credential_metadata"]
        scopes = self.cfg["vc-apigw"]["apigw"]["data_sources"]["datastore"]["scopes"]
        # no to_issuer psc entry: nothing uses it and vc#850 does not enforce one-psc-per-subject
        want_aux = {"to_wallet": [{"type": "hash_commitment", "alg": "sha-256", "path": SSC}]}
        want_derivation = [{"hash_commitment": {"path": SSC, "opening": {"hkdf": {
            "secret_path": SEED_PATH, "inputs": ["issuing_country", "personal_administrative_number"], "bytes": 16}}}}]
        for scope in ("nym_auth", "nym_auth_offer"):
            self.assertEqual(meta[scope]["auxiliary_data"], want_aux, scope)
            # EVERY data source scope serving the credential needs its to_wallet derivation, or the issuer refuses to start
            self.assertEqual(scopes[scope]["derivations"], want_derivation, scope)
        self.assertNotIn("auxiliary_data", meta["pid_1_8"])
        self.assertNotIn("derivations", scopes["pid_1_8"])

    def test_the_old_secret_shapes_are_gone(self):
        text = yaml.dump(self.cfg["vc-apigw"])
        for old in ("commitments", "secret_seed", "holder_commitments", "private_seed", "deliver:"):
            self.assertNotIn(old, text, old)

    def test_only_the_apigw_is_given_auxiliary_data(self):
        for name in ("vc-issuer", "vc-verifier", "vc-registry"):
            cfg = self.cfg[name]
            for scope, meta in (cfg.get("common", {}).get("credential_metadata") or {}).items():
                self.assertNotIn("auxiliary_data", meta, f"{name}/{scope}")
            for kind in ((cfg.get("apigw") or {}).get("data_sources") or {}).values():
                for scope in (kind.get("scopes") or {}).values():
                    self.assertNotIn("derivations", scope, name)

    def test_the_response_metadata_echo_stays_off(self):
        """emit_credential_metadata_on_response defaults to false and nothing here may switch it on."""
        delivery = self.cfg["vc-apigw"]["apigw"].get("delivery") or {}
        self.assertFalse(delivery.get("emit_credential_metadata_on_response", False))

    def test_the_issuer_publishes_credential_encryption_from_a_mounted_key(self):
        """vc REFUSES to start without apigw.issuer_metadata.credential_encryption.keys once a type declares auxiliary_data
        to_wallet: an opening travels only in an encrypted Credential Response."""
        enc = self.cfg["vc-apigw"]["apigw"]["issuer_metadata"]["credential_encryption"]
        self.assertEqual([k["private_key_path"] for k in enc["keys"]], [vc_render.CREDENTIAL_ENCRYPTION_PATH])
        self.assertFalse(enc["response_encryption_required"], "wallets that do no JWE must keep working")
        pem = (self.tmp / "secrets" / vc_render.CREDENTIAL_ENCRYPTION_FILE).read_text()
        self.assertIn("EC PRIVATE KEY", pem)
        for name in ("vc-issuer", "vc-verifier", "vc-registry"):
            self.assertNotIn("credential_encryption", (self.cfg[name].get("apigw") or {}).get("issuer_metadata") or {})

    def test_the_single_machine_layout_can_mount_the_encryption_key(self):
        from sirosid_core import singlemachine
        self.assertIn(vc_render.CREDENTIAL_ENCRYPTION_PATH, singlemachine.SECRET_PATHS["vc"])
        self.assertIn(f"vc-secrets/{vc_render.CREDENTIAL_ENCRYPTION_FILE}", [s for s, _ in singlemachine.SECRETS.values()])

    def test_the_issuer_advertises_its_notification_endpoint(self):
        meta = self.cfg["vc-apigw"]["apigw"]["issuer_metadata"]
        self.assertTrue(meta["notification_endpoint"].endswith("/notification"))

    def test_the_opening_secret_is_permanent_across_renders(self):
        name = vc_render.opening_secret_file(SEED_PATH)
        before = (self.tmp / "secrets" / name).read_text()
        rendered(self.tmp / "secrets", env_values=GDC_VALUES)
        self.assertEqual((self.tmp / "secrets" / name).read_text(), before, "a replaced secret repseudonymises everyone")

    def test_the_stored_documents_carry_no_ssc_or_psc(self):
        """vc refuses a document that already carries a value for a commitment claim."""
        for name in ("nym_auth.json", "nym_auth_offer.json"):
            for doc in json.loads((ROOT / "fixtures" / "vc-bootstrapping" / name).read_text()).values():
                self.assertFalse({"ssc", "psc"} & set(doc["document_data"]), name)


@NEEDS
class WithoutAuxiliaryData(unittest.TestCase):
    """The default: an environment whose vc image predates SUNET/vc#850 ignores auxiliary_data and derivations silently
    and would issue nym_auth with no ssc at all, so nothing is rendered for it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = rendered(cls.tmp / "secrets", documents=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_nothing_is_rendered(self):
        apigw = self.cfg["vc-apigw"]["apigw"]
        self.assertNotIn("credential_encryption", apigw["issuer_metadata"])
        for scope, meta in self.cfg["vc-apigw"]["common"]["credential_metadata"].items():
            self.assertNotIn("auxiliary_data", meta, scope)
        self.assertEqual(vc_render.opening_secret_paths(apigw), [])
        self.assertEqual([p.name for p in (self.tmp / "secrets").iterdir()
                          if p.name.startswith("vcOpening") and p.stat().st_size], [], "no real opening secret")

    def test_compose_still_has_files_for_its_bind_mounts(self):
        """docker-compose.vc-services.yml mounts both unconditionally; a missing source becomes a DIRECTORY."""
        for name in (vc_render.opening_secret_file(SEED_PATH), vc_render.CREDENTIAL_ENCRYPTION_FILE):
            path = self.tmp / "secrets" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.stat().st_size, 0, f"{name} is only a placeholder while the feature is off")

    def test_the_documents_carry_neither_ssc_nor_psc(self):
        self.assertEqual(set(self.cfg["documents"]), {"nym_auth.json", "nym_auth_offer.json"})
        for name, docs in self.cfg["documents"].items():
            for holder, doc in docs.items():
                self.assertFalse({"ssc", "psc"} & set(doc["document_data"]), f"{name}/{holder}")


if __name__ == "__main__":
    unittest.main()
