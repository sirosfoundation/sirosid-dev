#!/usr/bin/env python3
"""fixtures/vc-bootstrapping: the synthetic documents must agree with the
metadata they are issued against, and with the people they are issued to.

Four regressions, all found by a manual audit on 2026-09-18 and all silent -
nothing in the stack errors when a bootstrapping document is wrong, the wallet
just ends up with a credential it should never have been given, or with no
credential at all and no reason:

1. The three natural persons carried a different identity in their documents
   (Helen Mirren / Jason Momoa / Gary Oldman) than at login (Alice Wonderland /
   Bob Builder / Carol Danvers). PID-authenticated issuance resolves the holder
   by matching the presented PID's given_name/family_name/birth_date against
   identity_mappings.json, so none of them could obtain any PID-authenticated
   credential - and the failure is "no documents", which reads like a missing
   fixture rather than a mismatched one.
2. Derived age claims had gone stale: mdl said bob-002 was 42 when his birth
   date made him 43. They are recomputed here at test time, from birth_date,
   so they cannot rot again unnoticed.
3. mdl documents carried age_over_21/age_over_65, which the MDDL schema did not
   declare. An undeclared claim is dropped or rejected depending on the issuer,
   never flagged.
4. Every type issued documents of one single shape, so there was no way to test
   a credential carrying its optional claims. Each scope now has a minimal
   document and exactly one `-full` one; this pins that down.

    python3 -m unittest tests/test_fixture_integrity.py
"""
import datetime
import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_DIR = ROOT / "fixtures" / "vc-bootstrapping"
MAPPINGS = BOOTSTRAP_DIR / "identity_mappings.json"

# Matches any single array element; the VCTM spells it as a null path segment.
ANY = "[]"

# Scopes whose documents this suite deliberately does not check, and why.
#
#   pid_1_5   - no bootstrapping documents at all, by design: it is
#               assertion-sourced (values-base.yaml
#               features.credentialTypes.pid_1_5.issuance.source), built from
#               the mini-oidc claims at issuance time rather than from the
#               datastore. Out of scope for this file, and out of scope for
#               the audit that produced it.
#   ehic      - assertion-sourced for the same reason.
#
# Only datastore-sourced scopes appear in issuer.datastoreImport.documents, so
# both are skipped simply by not being there; they are named here so a future
# reader does not go looking for the fixture that "should" exist.
ASSERTION_SOURCED = {"pid_1_5", "ehic"}

# diploma's document_data is a whole embedded W3C Verifiable Credential
# (@context, credentialSubject, credentialSchema, ~30 nested fields) while its
# type metadata declares 7 claims, and its credentialSubject carries a third,
# unrelated identity. That divergence is deliberate and pre-existing - the
# documents are real EBSI/ELM sample data - so the claim-coverage and identity
# checks skip it. It still has to obey the `-full` marking convention.
EMBEDDED_VC_SCOPES = {"diploma"}

# Which claim in a document carries which attribute from identity_mappings.json.
# Keyed by scope; a path is a tuple, so nested claims can be named too.
IDENTITY_CLAIMS = {
    "pid_1_8": {("given_name",): "given_name",
                ("family_name",): "family_name",
                ("birthdate",): "birth_date"},
    "siros_id": {("given_name",): "given_name",
                 ("family_name",): "family_name",
                 ("birth_date",): "birth_date"},
    "mdl": {("given_name",): "given_name",
            ("family_name",): "family_name",
            ("birth_date",): "birth_date"},
    "pid_mdoc": {("given_name",): "given_name",
                 ("family_name",): "family_name"},
    "mdl_zk4": {("given_name",): "given_name",
                ("family_name",): "family_name"},
    # The EBW attestations describe a legal person, but three of them still
    # name the natural person holding the wallet.
    "iban_ov": {("account_ownership", "given_name"): "given_name",
                ("account_ownership", "surname"): "family_name"},
    "eu_poa": {("attorney_date_of_birth",): "birth_date"},
}

# Derived age claims, per scope: the birth-date claim, then each derived claim
# and the function that recomputes it from the age in whole years.
AGE_DERIVATIONS = {
    "pid_1_8": (("birthdate",), {
        ("age_in_years",): lambda age, year: age,
        ("age_birth_year",): lambda age, year: year,
        ("age_equal_or_over", "14"): lambda age, year: age >= 14,
        ("age_equal_or_over", "16"): lambda age, year: age >= 16,
        ("age_equal_or_over", "18"): lambda age, year: age >= 18,
        ("age_equal_or_over", "21"): lambda age, year: age >= 21,
        ("age_equal_or_over", "65"): lambda age, year: age >= 65,
    }),
    "mdl": (("birth_date",), {
        ("age_in_years",): lambda age, year: age,
        ("age_birth_year",): lambda age, year: year,
        ("age_over_18",): lambda age, year: age >= 18,
        ("age_over_21",): lambda age, year: age >= 21,
        ("age_over_65",): lambda age, year: age >= 65,
    }),
    "pid_mdoc": (None, {("age_over_18",): lambda age, year: age >= 18}),
    "mdl_zk4": (None, {("age_over_18",): lambda age, year: age >= 18}),
}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def load_values():
    return yaml.safe_load((ROOT / "values-base.yaml").read_text(encoding="utf-8"))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def declared_types(values):
    """{scope: type spec} for every type declared in values-base.yaml."""
    return values["features"]["credentialTypes"]


def bootstrapped_scopes(values):
    """{scope: Path} for every scope with a bootstrapping document file."""
    docs = values["issuer"]["datastoreImport"]["documents"]
    return {scope: ROOT / ref["file"] for scope, ref in docs.items()}


# ---------------------------------------------------------------------------
# claim paths
# ---------------------------------------------------------------------------
def vctm_paths(doc):
    """(all, mandatory) declared claim paths of a dc+sd-jwt VCTM."""
    all_paths, mandatory = set(), set()
    for claim in doc.get("claims", []):
        path = tuple(ANY if seg is None else seg for seg in claim["path"])
        all_paths.add(path)
        if claim.get("mandatory"):
            mandatory.add(path)
    return all_paths, mandatory


def mdoc_paths(doc):
    """(all, mandatory) declared element paths of an mdoc schema.

    Namespaces are dropped: a datastore document is a flat map of element
    names, not a namespaced one. An array element's own sub-elements are
    reached through an ANY segment, matching how the document nests them.
    """
    all_paths, mandatory = set(), set()

    def walk(spec, prefix):
        for name, sub in spec.items():
            path = prefix + (name,)
            all_paths.add(path)
            if sub.get("mandatory"):
                mandatory.add(path)
            if sub.get("elements"):
                all_paths.add(path + (ANY,))
                walk(sub["elements"], path + (ANY,))

    for elements in doc.get("claims", {}).values():
        walk(elements, ())
    return all_paths, mandatory


def declared_paths(spec):
    """(all, mandatory) claim paths a credential type declares."""
    if "mdocSchema" in spec:
        return mdoc_paths(load_json(ROOT / spec["mdocSchema"]["file"]))
    return vctm_paths(load_json(ROOT / spec["vctm"]["file"]))


def document_paths(value, prefix=()):
    """Every claim path present in one document_data value."""
    paths = set()
    if isinstance(value, dict):
        for key, sub in value.items():
            paths.add(prefix + (key,))
            paths |= document_paths(sub, prefix + (key,))
    elif isinstance(value, list):
        for item in value:
            paths.add(prefix + (ANY,))
            paths |= document_paths(item, prefix + (ANY,))
    return paths


def is_declared(path, declared):
    """True if `path` is declared, or sits under a declared opaque value.

    A claim declared with no declared children (eucc's legal_representative,
    iban_ov's legal_person_identifiers) is an opaque value the type metadata
    does not describe the inside of; whatever a document puts in there is the
    type's business, not a stray claim.
    """
    if path in declared:
        return True
    for cut in range(len(path) - 1, 0, -1):
        ancestor = path[:cut]
        if ancestor in declared:
            return not any(len(d) > cut and d[:cut] == ancestor for d in declared)
    return False


def dig(value, path):
    """Value at `path`, or KeyError. An ANY segment takes the first element."""
    for seg in path:
        if seg is ANY or seg == ANY:
            if not isinstance(value, list) or not value:
                raise KeyError(path)
            value = value[0]
        else:
            if not isinstance(value, dict) or seg not in value:
                raise KeyError(path)
            value = value[seg]
    return value


def has(value, path):
    try:
        dig(value, path)
    except KeyError:
        return False
    return True


def age_on(birth_date, today):
    born = datetime.date.fromisoformat(birth_date)
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = load_values()
        cls.types = declared_types(cls.values)
        cls.scopes = bootstrapped_scopes(cls.values)
        cls.documents = {scope: load_json(path) for scope, path in cls.scopes.items()}
        cls.mappings = load_json(MAPPINGS)


class DeclaredTypes(FixtureCase):
    def test_every_bootstrapped_scope_is_a_declared_type(self):
        for scope in self.scopes:
            self.assertIn(scope, self.types,
                          f"{scope}.json is imported but features.credentialTypes "
                          f"declares no such type")

    def test_assertion_sourced_types_have_no_documents(self):
        for scope in ASSERTION_SOURCED:
            self.assertNotIn(scope, self.scopes,
                             f"{scope} is assertion-sourced; a datastore document "
                             f"for it would never be read")


class ClaimsAreDeclared(FixtureCase):
    """Finding 4, in reverse: nothing may be issued that no metadata declares."""

    def test_every_document_claim_is_declared(self):
        for scope, docs in self.documents.items():
            if scope in EMBEDDED_VC_SCOPES:
                continue
            declared, _ = declared_paths(self.types[scope])
            for holder, doc in docs.items():
                for path in sorted(document_paths(doc["document_data"])):
                    self.assertTrue(
                        is_declared(path, declared),
                        f"{scope}.json[{holder}] issues {'.'.join(path)}, which "
                        f"{scope}'s type metadata does not declare")


class MinimalAndFullDocuments(FixtureCase):
    """Finding 6: one thin document per type, and exactly one fat one."""

    def test_exactly_one_full_document_per_scope(self):
        for scope, docs in self.documents.items():
            full = [h for h, d in docs.items()
                    if d["meta"]["document_id"].endswith("-full")]
            self.assertEqual(
                len(full), 1,
                f"{scope}.json must mark exactly one document `-full` "
                f"(mandatory + every optional claim), found {full or 'none'}")

    def test_full_document_covers_every_declared_claim(self):
        for scope, docs in self.documents.items():
            if scope in EMBEDDED_VC_SCOPES:
                continue
            declared, _ = declared_paths(self.types[scope])
            for holder, doc in docs.items():
                if not doc["meta"]["document_id"].endswith("-full"):
                    continue
                missing = sorted(p for p in declared
                                 if not has(doc["document_data"], p))
                self.assertEqual(
                    missing, [],
                    f"{scope}.json[{holder}] is marked `-full` but omits "
                    f"{['.'.join(p) for p in missing]}")

    def test_some_document_covers_every_mandatory_claim(self):
        for scope, docs in self.documents.items():
            if scope in EMBEDDED_VC_SCOPES:
                continue
            _, mandatory = declared_paths(self.types[scope])
            covering = [h for h, d in docs.items()
                        if all(has(d["document_data"], p) for p in mandatory)]
            self.assertTrue(
                covering,
                f"no {scope} document covers every mandatory claim "
                f"({sorted('.'.join(p) for p in mandatory)}) - the type cannot "
                f"be issued at all")

    def test_one_document_per_holder_per_scope(self):
        # Two documents of one scope for one holder makes issuance ambiguous:
        # nothing picks between them.
        for scope, docs in self.documents.items():
            holders = [h for doc in docs.values()
                       for h in doc["identity_mapping_ids"]]
            self.assertEqual(sorted(holders), sorted(set(holders)),
                             f"{scope}.json has two documents for one holder")


class DerivedAges(FixtureCase):
    """Finding 2: an age claim is a function of the birth date, recomputed here.

    This fails the day a stored age falls behind the person's real one, which
    is the point: the values are derived data with no other guard.
    """

    def test_ages_match_birth_date(self):
        today = datetime.date.today()
        for scope, (birth_path, derivations) in AGE_DERIVATIONS.items():
            docs = self.documents.get(scope, {})
            for holder, doc in docs.items():
                data = doc["document_data"]
                if birth_path is not None:
                    birth = dig(data, birth_path)
                else:
                    # pid_mdoc/mdl_zk4 carry no birth date of their own; the
                    # holder's mapping is the source of truth.
                    birth = self.mappings[holder][0]["attributes"]["birth_date"]
                age = age_on(birth, today)
                year = int(birth[:4])
                for path, derive in derivations.items():
                    if not has(data, path):
                        continue
                    self.assertEqual(
                        dig(data, path), derive(age, year),
                        f"{scope}.json[{holder}].{'.'.join(path)} disagrees with "
                        f"birth date {birth} (age {age} today) - recompute it")


class IdentityMatchesMappings(FixtureCase):
    """Finding 1: a document's holder must be the person who logs in."""

    def test_every_holder_has_a_mapping(self):
        for scope, docs in self.documents.items():
            for holder, doc in docs.items():
                self.assertIn(holder, self.mappings,
                              f"{scope}.json[{holder}] has no identity mapping; "
                              f"PID-authenticated issuance can never resolve it")
                for mapped in doc["identity_mapping_ids"]:
                    self.assertIn(mapped, self.mappings,
                                  f"{scope}.json[{holder}] maps to unknown "
                                  f"identity {mapped}")

    def test_every_mapping_has_a_birth_date(self):
        # Without one, an openid4vp issuance flow matching on
        # given_name/family_name/birthdate cannot resolve the holder.
        for holder, entries in self.mappings.items():
            for entry in entries:
                self.assertIn("birth_date", entry["attributes"],
                              f"identity_mappings.json[{holder}] has no birth_date")

    def test_identity_claims_match_the_mapping(self):
        for scope, claims in IDENTITY_CLAIMS.items():
            for holder, doc in self.documents.get(scope, {}).items():
                attributes = self.mappings[holder][0]["attributes"]
                for path, attribute in claims.items():
                    if not has(doc["document_data"], path):
                        continue
                    self.assertEqual(
                        dig(doc["document_data"], path), attributes[attribute],
                        f"{scope}.json[{holder}].{'.'.join(path)} is not the "
                        f"{attribute} {holder} logs in with")

    def test_pid_birth_names_match_current_names(self):
        # Not a rule in general - people change names - but these personas do
        # not, and a birth name left over from a previous persona is exactly
        # how finding 1 stayed hidden.
        for holder, doc in self.documents["pid_1_8"].items():
            data = doc["document_data"]
            self.assertEqual(data["birth_given_name"], data["given_name"],
                             f"pid_1_8.json[{holder}] birth_given_name drifted")
            self.assertEqual(data["birth_family_name"], data["family_name"],
                             f"pid_1_8.json[{holder}] birth_family_name drifted")


if __name__ == "__main__":
    unittest.main()
