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
import re
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
EMBEDDED_VC_SCOPES = {"diploma", "diploma_offer"}

# Claims a stored document must NOT carry although the type declares them: the issuer derives every
# auxiliary_data `to_wallet` claim (ssc) with a hash_commitment derivation and refuses a document that already has
# one, and psc (`to_issuer`) is supplied by the wallet at issuance, not stored. (SUNET/vc#850)
HOLDER_SUPPLIED = {"psc"}


def commitment_claims(spec):
    """The claims a type's `auxiliaryData.to_wallet` has the issuer generate (the last element of each path)."""
    return {(e["path"][-1],) for e in ((spec.get("auxiliaryData") or {}).get("to_wallet") or [])}


def subject_inputs(spec):
    """Document paths a type's hash_commitment openings read. They live in the stored document as inputs only:
    the type's schema does not declare them, so they are never issued."""
    paths = set()
    for d in spec.get("derivations") or []:
        opening = (d.get("hash_commitment") or {}).get("opening") or {}
        for how in ("hkdf", "hmac"):
            for i in (opening.get(how) or {}).get("inputs", []):
                paths.add(tuple(i.split(".")))
    return paths


def generated_or_supplied(spec):
    return commitment_claims(spec) | {(c,) for c in HOLDER_SUPPLIED}

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
    "photoid": {("given_name_unicode",): "given_name",
                ("family_name_unicode",): "family_name",
                ("birth_date",): "birth_date"},
    # The EBW attestations describe a legal person, but three of them still
    # name the natural person holding the wallet.
    "iban_ov": {("account_ownership", "given_name"): "given_name",
                ("account_ownership", "surname"): "family_name"},
    "eu_poa": {("attorney_date_of_birth",): "birth_date"},
    "nym_auth": {("birth_date",): "birth_date"},
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
    "photoid": (("birth_date",), {
        ("age_in_years",): lambda age, year: age,
        ("age_birth_year",): lambda age, year: year,
        ("age_over_18",): lambda age, year: age >= 18,
    }),
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


def claim_descriptions(spec):
    """{path: description} for every claim whose metadata describes it.

    The descriptions are where a type says what a value has to *look* like -
    which country-code standard, which identifier scheme - and nothing else
    in the stack reads them.
    """
    described = {}
    if "mdocSchema" in spec:
        doc = load_json(ROOT / spec["mdocSchema"]["file"])

        def walk(elements, prefix):
            for name, sub in elements.items():
                path = prefix + (name,)
                if sub.get("description"):
                    described[path] = sub["description"]
                if sub.get("elements"):
                    walk(sub["elements"], path + (ANY,))

        for elements in doc.get("claims", {}).values():
            walk(elements, ())
        return described
    for claim in load_json(ROOT / spec["vctm"]["file"]).get("claims", []):
        if claim.get("description"):
            described[tuple(ANY if seg is None else seg for seg in claim["path"])] = claim["description"]
    return described


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
                    if path in subject_inputs(self.types[scope]):
                        continue
                    self.assertTrue(
                        is_declared(path, declared),
                        f"{scope}.json[{holder}] issues {'.'.join(path)}, which "
                        f"{scope}'s type metadata does not declare")


class CodedValuesMatchTheirStandard(FixtureCase):
    """A claim's description names the code list its value comes from, and a
    value from the neighbouring list is the kind of wrong nothing catches: an
    ISO 3166-1 country where ISO 3166-2 asks for a subdivision is still two
    plausible letters, and the credential issues, renders and presents. Found
    in eucc.json, where two of four documents said `SE`/`FR` for
    `issuing_jurisdiction`; the same claim was corrected in pid_1_8 and mdl.
    """

    # Only values that already look like a code are judged. Several
    # descriptions offer a name *or* a code ("Name of the administrative
    # authority ... or ISO 3166-1 alpha-2 where applicable"), and a name is
    # then right; what no reader catches is one code list used where another
    # was asked for, and that is what this compares.
    CODE_SHAPED = re.compile(r"^[A-Za-z0-9-]{2,6}$")

    STANDARDS = (
        # (every term the description must contain, pattern, what it is)
        (("3166-2",), re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$"),
         "an ISO 3166-2 subdivision code, e.g. SE-AB - not a bare country"),
        (("3166-1", "alpha-2"), re.compile(r"^[A-Z]{2}$"),
         "an ISO 3166-1 alpha-2 country code"),
    )

    def test_values_match_the_standard_their_description_names(self):
        for scope, docs in self.documents.items():
            if scope in EMBEDDED_VC_SCOPES:
                continue
            described = claim_descriptions(self.types[scope])
            for path, description in described.items():
                lowered = description.lower()
                for terms, pattern, what in self.STANDARDS:
                    if not all(term in lowered for term in terms):
                        continue
                    for holder, doc in docs.items():
                        if not has(doc["document_data"], path):
                            continue
                        value = dig(doc["document_data"], path)
                        if not isinstance(value, str) or not self.CODE_SHAPED.match(value):
                            continue
                        self.assertRegex(
                            value, pattern,
                            f"{scope}.json[{holder}].{'.'.join(str(p) for p in path)} "
                            f"is {value!r}; its type metadata asks for {what}")
                    break


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
                                 if not has(doc["document_data"], p)
                                 and p not in generated_or_supplied(self.types[scope]))
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
                        if all(has(d["document_data"], p) for p in mandatory
                               if p not in generated_or_supplied(self.types[scope]))]
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


class NymAuth(FixtureCase):
    """nym_auth (nym_auth_rulebook v0.3) - issued behind a presented PID, mDL or
    Photo ID, with the birth date of the presented credential.

    vc main cannot copy a presented claim into a datastore document, so the
    document's own birth_date is what vc main issues; these tests hold it equal
    to the birth date every presentable credential of the same person carries
    - the value a vc with auth_scopes.<x>.document_claims copies instead - so
    both builds issue the same date.
    """

    SCOPE = "nym_auth"
    NAMESPACE = "eu.europa.ec.eudi.nym_auth.1"
    AUTH_SCOPES = {"pid_1_8", "pid_1_5", "pid_mdoc", "mdl", "photoid"}
    NATURAL_PERSONS = {"alice-001", "bob-002", "carol-003"}
    PRIMARY_MDOC_NAMESPACE = "org.iso.18013.5.1"

    # Source credentials whose own schema cannot supply the birth date yet.
    # vc with document_claims refuses such a presentation (fail closed) rather
    # than issue the stored date - so the option is unusable until the gap is
    # closed, and this test fails the day it is, to have the entry removed.
    BIRTH_DATE_GAPS = {
        "pid_mdoc": "fixtures/vc-metadata/pid_mdoc.mdoc.json is a 4-claim stub without the EU PID's birth_date",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.spec = cls.types[cls.SCOPE]
        cls.auth = cls.spec["issuance"]["authOptions"]["scopes"]
        cls.schema = load_json(ROOT / cls.spec["mdocSchema"]["file"])
        cls.docs = cls.documents[cls.SCOPE]

    # -- the type -----------------------------------------------------------
    def test_type_is_the_rulebooks_mdoc(self):
        self.assertEqual(self.spec["format"], "mso_mdoc")
        self.assertEqual(self.spec["doctype"], self.NAMESPACE)
        self.assertEqual(self.schema["doctype"], self.NAMESPACE)
        self.assertEqual(self.schema["format"], "mso_mdoc")
        # Document type and namespace MUST both be eu.europa.ec.eudi.nym_auth.1.
        self.assertEqual(list(self.schema["claims"]), [self.NAMESPACE])
        claims = self.schema["claims"][self.NAMESPACE]
        self.assertEqual({k: (v.get("value_type"), bool(v.get("mandatory"))) for k, v in claims.items()},
                         {"birth_date": ("full-date", True), "ssc": ("bstr", True), "psc": ("bstr", False)})

    def test_issued_behind_openid4vp_from_the_datastore(self):
        issuance = self.spec["issuance"]
        self.assertEqual((issuance["authProvider"], issuance["source"]), ("openid4vp", "datastore"))
        self.assertEqual(set(self.auth), self.AUTH_SCOPES)
        for scope in self.auth:
            self.assertIn(scope, self.types, f"auth scope {scope} is no declared credential type")

    # -- the documents ------------------------------------------------------
    def test_one_document_per_natural_person(self):
        self.assertEqual(set(self.docs), self.NATURAL_PERSONS)
        for holder, doc in self.docs.items():
            self.assertEqual(doc["identity_mapping_ids"], [holder])
            self.assertEqual(doc["meta"]["scope"], self.SCOPE)

    def test_documents_carry_the_birth_date_and_the_commitment_subjects_only(self):
        # ssc is derived by the issuer (a pre-set one is refused) and psc is wallet-supplied, so neither is
        # stored; issuing_country and personal_administrative_number are the opening's inputs.
        want = {"birth_date", "issuing_country", "personal_administrative_number"}
        for holder, doc in self.docs.items():
            self.assertEqual(set(doc["document_data"]), want, holder)

    def test_ssc_is_derived_by_the_issuer_and_delivered_for_both_scopes(self):
        # SUNET/vc#850: ssc is an auxiliary_data `to_wallet` hash_commitment the issuer fulfils with one hkdf derivation
        # per data source scope, psc a `to_issuer` one the wallet supplies; both claims are declared by the schema and
        # psc is NOT mandatory there, so a wallet that sends none is still served.
        for scope in (self.SCOPE, self.SCOPE + "_offer"):
            aux = self.types[scope].get("auxiliaryData") or {}
            self.assertEqual([e["path"] for e in aux.get("to_wallet", [])], [[self.NAMESPACE, "ssc"]], scope)
            self.assertEqual([e["path"] for e in aux.get("to_issuer", [])], [[self.NAMESPACE, "psc"]], scope)
            for e in aux["to_wallet"] + aux["to_issuer"]:
                self.assertEqual((e["type"], e["alg"]), ("hash_commitment", "sha-256"), scope)
                self.assertNotIn("required", e, "vc derives `required` from the MDDL's mandatory flag and refuses it here")
            derivation = [d["hash_commitment"] for d in self.types[scope]["derivations"]]
            self.assertEqual([d["path"] for d in derivation], [[self.NAMESPACE, "ssc"]], scope)
            self.assertEqual(self.types[scope]["derivations"], self.spec["derivations"],
                             f"{scope} must derive the same opening as {self.SCOPE}: one person, one pseudonym")
            self.assertEqual(derivation[0]["opening"]["hkdf"]["bytes"], 16, scope)
        self.assertIn("ssc", self.schema["claims"][self.NAMESPACE], "the schema must declare the claim, or the signer drops it")
        self.assertFalse(self.schema["claims"][self.NAMESPACE]["psc"]["mandatory"],
                         "a mandatory psc refuses a wallet that sends none (the old APK sends none)")

    def test_no_document_carries_ssc_or_psc(self):
        for holder, doc in self.docs.items():
            for claim in ("ssc", "psc"):
                self.assertNotIn(claim, doc["document_data"], f"{holder}: vc refuses a document that already has {claim}")

    def test_commitment_subjects_are_the_persons_pid_identifiers(self):
        # Same inputs as the person's PID, so the pseudonym is the same whichever credential asks.
        pid = self.documents["pid_1_8"]
        subjects = self.spec["derivations"][0]["hash_commitment"]["opening"]["hkdf"]["inputs"]
        self.assertEqual(subjects, ["issuing_country", "personal_administrative_number"])
        for holder, doc in self.docs.items():
            for s in subjects:
                self.assertEqual(doc["document_data"][s], pid[holder]["document_data"][s], f"{holder}.{s}")
                self.assertTrue(doc["document_data"][s], f"{holder}.{s} must be non-empty (vc refuses an empty subject)")

    # -- the birth date -----------------------------------------------------
    def source_birth_date(self, auth_scope, holder):
        """The birth date `holder`'s `auth_scope` credential carries at the
        claim nym_auth's documentClaims copies from, as a date - or None when
        the holder has no such credential."""
        # The SD-JWT PID scopes also copy the national identifier and issuing country (the seed's subjects); this is about the date.
        (source,) = [src for src, dst in self.auth[auth_scope]["documentClaims"].items() if dst == "birth_date"]
        spec = self.types[auth_scope]
        if spec["issuance"]["source"] == "assertion":
            # pid_1_5 is built from mini-oidc's own claims at issuance; its
            # `birthdate` is mini-oidc's, which identity_mappings.json mirrors.
            self.assertEqual(source, "birthdate")
            value = self.mappings[holder][0]["attributes"]["birth_date"]
        else:
            if spec["format"] == "mso_mdoc":
                path = (mdoc_element(source, load_json(ROOT / spec["mdocSchema"]["file"]))[1],)
            else:
                path = tuple(source.split("."))
            doc = self.documents.get(auth_scope, {}).get(holder)
            if doc is None:
                return None
            value = dig(doc["document_data"], path)
        return normalise_full_date(value)

    def test_documents_birth_date_is_the_presented_credentials(self):
        for holder, doc in self.docs.items():
            issued = doc["document_data"]["birth_date"]
            # The canonical RFC 3339 full-date the mdoc issuer tags 1004.
            self.assertEqual(normalise_full_date(issued).isoformat(), issued, holder)
            self.assertEqual(issued, self.mappings[holder][0]["attributes"]["birth_date"],
                             f"nym_auth.json[{holder}] is not {holder}'s mini-oidc birth date")
            seen = 0
            for auth_scope in sorted(self.AUTH_SCOPES - set(self.BIRTH_DATE_GAPS)):
                presented = self.source_birth_date(auth_scope, holder)
                if presented is None:
                    continue
                seen += 1
                self.assertEqual(presented.isoformat(), issued,
                                 f"{holder} presenting {auth_scope} would be issued {presented}, "
                                 f"but nym_auth.json says {issued}")
            self.assertGreaterEqual(seen, 3, f"{holder} can present too few birth-date sources")

    def test_mini_oidc_users_agree_when_the_checkout_is_there(self):
        users = ROOT.parent / "mini-oidc" / "users.yaml"
        if not users.exists():
            self.skipTest("no ../mini-oidc checkout")
        by_sub = {u["sub"]: u for u in yaml.safe_load(users.read_text(encoding="utf-8"))["users"]}
        for holder, doc in self.docs.items():
            self.assertEqual(str(by_sub[holder]["birthdate"]), doc["document_data"]["birth_date"], holder)

    # -- what vc is asked for -----------------------------------------------
    def test_every_copied_claim_is_declared_by_its_source_credential(self):
        for auth_scope, entry in self.auth.items():
            for source in entry.get("documentClaims", {}):
                declared = claim_declared(self.types[auth_scope], source)
                if auth_scope in self.BIRTH_DATE_GAPS:
                    self.assertFalse(declared, f"{auth_scope} now declares {source}: drop it from "
                                               f"BIRTH_DATE_GAPS and the values-base.yaml comment")
                else:
                    self.assertTrue(declared, f"{auth_scope} does not declare {source}")

    def test_every_auth_claim_resolves_and_is_a_string(self):
        # vc main refuses a non-string identity claim, and an mdoc full-date
        # arrives as a CBOR tag 1004 - so no mdoc date may be an auth claim.
        for auth_scope, entry in self.auth.items():
            spec = self.types[auth_scope]
            for claim in entry["claims"]:
                self.assertTrue(claim_declared(spec, claim), f"{auth_scope}: {claim} is not declared")
                if spec["format"] == "mso_mdoc":
                    ns, element = mdoc_element(claim, load_json(ROOT / spec["mdocSchema"]["file"]))
                    meta = load_json(ROOT / spec["mdocSchema"]["file"])["claims"][ns][element]
                    self.assertEqual(meta.get("value_type"), "tstr", f"{auth_scope}: {claim}")
            for claim, attribute in entry.get("identityAttributes", {}).items():
                self.assertIn(claim, entry["claims"], auth_scope)
                self.assertIn(attribute, {"given_name", "family_name", "birth_date"}, auth_scope)

    def test_identity_resolves_to_the_mapping_attributes(self):
        # What ResolveIdentifier matches on is the auth claim renamed by
        # identityAttributes: every option has to reach given_name AND
        # family_name, or it cannot select a person.
        for auth_scope, entry in self.auth.items():
            names = {entry.get("identityAttributes", {}).get(c, c) for c in entry["claims"]}
            self.assertLessEqual({"given_name", "family_name"}, names, auth_scope)


def mdoc_element(claim, schema):
    """(namespace, element) an mdoc auth claim names, the way vc's
    authClaimPathSegments reads it: "<namespace>.<element>" for a namespace the
    schema has, a bare element for org.iso.18013.5.1."""
    for ns in schema.get("claims", {}):
        if claim.startswith(ns + "."):
            return ns, claim[len(ns) + 1:]
    return NymAuth.PRIMARY_MDOC_NAMESPACE, claim


def claim_declared(spec, claim):
    if "mdocSchema" in spec:
        schema = load_json(ROOT / spec["mdocSchema"]["file"])
        ns, element = mdoc_element(claim, schema)
        return element in schema.get("claims", {}).get(ns, {})
    all_paths, _ = vctm_paths(load_json(ROOT / spec["vctm"]["file"]))
    return tuple(claim.split(".")) in all_paths


def normalise_full_date(value):
    """A presented birth date as a date: a full-date, or a date-time's date
    (the shapes vc's normaliseFullDate accepts once CBOR tags are unwrapped)."""
    if isinstance(value, datetime.date):
        return value if not isinstance(value, datetime.datetime) else value.date()
    text = str(value)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).date()


if __name__ == "__main__":
    unittest.main()


class PreAuthorizedTwins(FixtureCase):
    """`<type>_offer` is the same credential as `<type>` behind auth_provider: preauth, so a pre-authorized
    offer can be minted (vc refuses any other scope). The copies must not drift from their source."""

    def twins(self):
        return [s for s in self.types if s.endswith("_offer")]

    def test_there_are_twins(self):
        self.assertTrue(self.twins())

    def test_every_datastore_type_has_a_twin(self):
        for s, d in self.types.items():
            if s.endswith("_offer") or d["issuance"]["source"] != "datastore":
                continue
            self.assertIn(s + "_offer", self.types, f"{s} is datastore-sourced but cannot be offered: no {s}_offer twin")

    def test_twin_is_preauth_and_the_same_credential(self):
        for s in self.twins():
            src = s[: -len("_offer")]
            self.assertEqual(self.types[s]["issuance"]["authProvider"], "preauth", s)
            for k in ("vct", "doctype", "format", "vctm", "mdocSchema"):
                self.assertEqual(self.types[s].get(k), self.types[src].get(k), f"{s}.{k} differs from {src}")

    def test_twin_documents_equal_the_sources(self):
        for s in self.twins():
            src = s[: -len("_offer")]
            self.assertEqual(set(self.documents[s]), set(self.documents[src]), f"{s} holders")
            for holder, doc in self.documents[s].items():
                want = json.loads(json.dumps(self.documents[src][holder]))
                want["meta"]["scope"] = s
                want["meta"]["document_id"] = want["meta"]["document_id"].replace(f"_{src}_", f"_{s}_", 1)
                self.assertEqual(doc, want, f"{s}/{holder} drifted from {src}")
