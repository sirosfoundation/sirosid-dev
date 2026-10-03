import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import policy  # noqa: E402
from sirosid_core.components import component_names  # noqa: E402
from sirosid_core.policy import (CAP_CUSTOM_IMAGES, CAP_RAW_VALUES, PlatformPolicy, PolicyError, Problem,  # noqa: E402
                                 build_spec, check_https_url, check_identity, check_image, parse_image, validate)

PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----"
HEX = "AA:BB:CC:DD:EE:FF:00:11"


def paths(problems):
    return sorted(p.path for p in problems)


def one(check, value):
    problems = []
    ok = check(value, "x", problems)
    return ok, problems


class UrlTests(unittest.TestCase):
    def test_public_https_is_accepted(self):
        for u in ("https://issuer.example.com", "https://issuer.example.com:8443/path?q=1", "https://8.8.8.8/",
                  "https://a.b.c.example.org/.well-known/x"):
            self.assertTrue(one(check_https_url, u)[0], u)

    def test_everything_that_could_reach_inside_is_refused(self):
        for u in ("http://issuer.example.com", "https://localhost", "https://127.0.0.1", "https://10.0.0.5/",
                  "https://192.168.1.1", "https://169.254.169.254/latest", "https://[::1]/", "https://[fd00::1]/",
                  "https://sid-x-pdp.internal", "https://pdp.flycast", "https://intranet", "https://user:pw@example.com",
                  "https://", "ftp://example.com", "javascript:alert(1)", "", "https://example.com:99999", None, 7):
            ok, probs = one(check_https_url, u)
            self.assertFalse(ok, u)
            self.assertEqual(len(probs), 1, u)

    def test_overlong_is_refused(self):
        self.assertFalse(one(check_https_url, "https://example.com/" + "a" * 3000)[0])


class IdentityTests(unittest.TestCase):
    def test_accepted_forms(self):
        for v in ("https://verifier.example.com", "x509_hash:abcDEF123-_", "x509_san_dns:verifier.example.com",
                  "decentralized_identifier:did:web:acra.uat.accredify.io"):
            self.assertTrue(one(check_identity, v)[0], v)

    def test_refused_forms(self):
        for v in ("http://x.example.com", "plain text", "x509_hash:has space", "x509_hash:" + "a" * 600, "", None,
                  "https://localhost"):
            self.assertFalse(one(check_identity, v)[0], v)


class PemTests(unittest.TestCase):
    def test_certificate_only(self):
        self.assertTrue(one(policy.check_pem, PEM)[0])
        for bad in ("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----", "hello", "", None, PEM + "\n" + "A" * 20000,
                    PEM.replace("fake", "-----BEGIN EC PRIVATE KEY-----")):
            self.assertFalse(one(policy.check_pem, bad)[0], str(bad)[:30])


class ImageTests(unittest.TestCase):
    GOOD = ("ghcr.io/sirosfoundation/vc/apigw:0.7.0", "docker.io/library/nginx:alpine", "registry.gitlab.com/a/b@sha256:" + "a" * 64,
            "ghcr.io/org/img:1.2@sha256:" + "b" * 64, "reg.example.com:5000/team/app:v1")
    BAD = ("nginx:alpine", "mongo:7", "ghcr.io/org/img", "ghcr.io/org/IMG:tag", "ghcr.io/org/img:", "", "ghcr.io//img:1",
           "ghcr.io/org/img@sha256:short", "has space/img:1", None)

    def test_parse_accepts_qualified_pinned_refs(self):
        for r in self.GOOD:
            self.assertEqual(str(parse_image(r)), r)

    def test_parse_rejects_bare_and_unpinned(self):
        for r in self.BAD:
            with self.assertRaises(ValueError, msg=str(r)):
                parse_image(r)

    def test_the_bare_name_message_explains_the_docker_daemon(self):
        with self.assertRaises(ValueError) as cm:
            parse_image("wallet-backend-e2e-test:local")
        self.assertIn("local docker daemon", str(cm.exception))

    def test_pinned_property(self):
        self.assertTrue(parse_image(self.GOOD[2]).pinned)
        self.assertFalse(parse_image(self.GOOD[0]).pinned)

    def test_other_apps_fly_registry_and_internal_hosts_are_refused(self):
        pol = PlatformPolicy()
        for r in ("registry.fly.io/sirosid-gdc-vc-apigw:local-1", "localhost:5000/x:1", "10.0.0.1/x:1", "reg.internal/x:1"):
            self.assertFalse(one(lambda v, p, pr: check_image(v, p, pol, pr), r)[0], r)

    def test_registry_allowlist(self):
        pol = PlatformPolicy(allowed_image_registries=("ghcr.io",))
        chk = lambda v, p, pr: check_image(v, p, pol, pr)
        self.assertTrue(one(chk, "ghcr.io/org/i:1")[0])
        self.assertFalse(one(chk, "docker.io/library/nginx:1")[0])


class ValidateTests(unittest.TestCase):
    def test_empty_config_is_valid(self):
        self.assertEqual(validate({}), [])

    def test_unknown_keys_are_rejected_not_ignored(self):
        self.assertEqual(paths(validate({"trusted_issuer": ["https://a.example.com"], "region": "iad"})),
                         ["region", "trusted_issuer"])

    def test_the_platforms_own_settings_cannot_be_chosen_by_a_user(self):
        for k in ("region", "org", "app_prefix", "host_pattern", "scale_to_zero", "env"):
            self.assertTrue(validate({k: "x"}), k)

    def test_images_need_the_capability(self):
        cfg = {"images": {"pdp": "ghcr.io/me/pdp:1"}}
        probs = validate(cfg)
        self.assertEqual(paths(probs), ["images"])
        self.assertIn(CAP_CUSTOM_IMAGES, probs[0].message)
        self.assertEqual(validate(cfg, [CAP_CUSTOM_IMAGES]), [])

    def test_the_images_capability_does_not_unlock_values(self):
        probs = validate({"values": {"a": 1}}, [CAP_CUSTOM_IMAGES])
        self.assertEqual(paths(probs), ["values"])
        self.assertEqual(validate({"values": {"a": 1}}, [CAP_RAW_VALUES]), [])

    def test_images_must_name_a_component_and_a_good_reference(self):
        cfg = {"images": {"nonsense": "ghcr.io/a/b:1", "pdp": "nginx:alpine", "wallet-backend": "ghcr.io/a/b:1"}}
        self.assertEqual(paths(validate(cfg, [CAP_CUSTOM_IMAGES])), ["images.nonsense", "images.pdp"])

    def test_every_problem_is_reported_at_once(self):
        cfg = {"trusted_issuers": ["http://a.example.com", "https://localhost"], "trusted_verifiers": ["nope"],
               "credential_registries": ["https://10.0.0.1"], "android_apps": ["bad"], "wallet_attestation": "yes",
               "dc_api_enable": "maybe", "mystery": 1}
        got = paths(validate(cfg))
        self.assertEqual(got, ["android_apps", "credential_registries[0]", "dc_api_enable", "mystery",
                               "trusted_issuers[0]", "trusted_issuers[1]", "trusted_verifiers[0]", "wallet_attestation"])

    def test_conformance_is_off_unless_the_platform_allows_it(self):
        self.assertEqual(paths(validate({"conformance": True})), ["conformance"])
        self.assertEqual(validate({"conformance": True}, policy=PlatformPolicy(allow_conformance=True)), [])
        self.assertEqual(validate({"conformance": False}), [])

    def test_channel_must_exist(self):
        self.assertEqual(paths(validate({"channel": "nightly"})), ["channel"])
        pol = PlatformPolicy(channels={"default": {}, "nightly": {}})
        self.assertEqual(validate({"channel": "nightly"}, policy=pol), [])

    def test_limits(self):
        self.assertTrue(validate({"trusted_issuers": ["https://a.example.com"] * 51}))
        self.assertTrue(validate({"name": "x" * 65}))
        self.assertTrue(validate({"android_apps": [f"p{i}={HEX}" for i in range(21)]}))

    def test_android_entries(self):
        self.assertEqual(validate({"android_apps": [f"org.x={HEX}"]}), [])
        self.assertTrue(validate({"android_apps": ["no-equals"]}))
        self.assertTrue(validate({"android_apps": [f"org.x=ZZ:ZZ"]}))
        self.assertTrue(validate({"android_apps": [7]}))

    def test_not_an_object(self):
        self.assertEqual(validate([]), [Problem("$", "a saved config must be a JSON object")])

    def test_schema_version(self):
        self.assertTrue(validate({"schema_version": 2}))
        self.assertTrue(validate({"schema_version": True}))
        self.assertEqual(validate({"schema_version": 1}), [])


class SchemaTests(unittest.TestCase):
    def test_schema_lists_exactly_the_keys_validate_knows(self):
        self.assertEqual(sorted(policy.schema()["properties"]), sorted(policy.SAVED_CONFIG_KEYS))
        for key in policy.SAVED_CONFIG_KEYS:
            unknown = [p for p in validate({key: None}) if "not a known key" in p.message]
            self.assertEqual(unknown, [], key)

    def test_schema_forbids_additional_properties(self):
        self.assertIs(policy.schema()["additionalProperties"], False)

    def test_a_key_with_no_effect_is_not_in_the_schema(self):
        self.assertNotIn("credential_types", policy.SAVED_CONFIG_KEYS)


class BuildSpecTests(unittest.TestCase):
    def test_platform_settings_are_applied_and_not_overridable(self):
        pol = PlatformPolicy(region="fra", app_prefix="sid", host_pattern="{env}-{component}.dev.example", scale_to_zero=True)
        spec = build_spec({"name": "x"}, "k7m2qx4d", policy=pol)
        self.assertEqual((spec.env, spec.region, spec.app_prefix, spec.host_pattern, spec.scale_to_zero),
                         ("k7m2qx4d", "fra", "sid", "{env}-{component}.dev.example", True))

    def test_user_fields_flow_through(self):
        spec = build_spec({"trusted_issuers": ["https://i.example.com"], "trusted_verifiers": ["x509_hash:abc"],
                           "credential_registries": ["https://r.example.com"], "android_apps": [f"org.x={HEX}"],
                           "trusted_verifier_roots": [PEM], "wallet_attestation": True, "dc_api_enable": "true"}, "e")
        self.assertEqual(spec.trusted_issuers, ["https://i.example.com"])
        self.assertEqual(spec.android_apps, [f"org.x={HEX}"])
        self.assertTrue(spec.wallet_attestation)
        self.assertEqual(spec.dc_api_enable, "true")
        self.assertTrue(spec.trusted_verifier_roots[0].endswith("-----END CERTIFICATE-----\n"))
        self.assertFalse(spec.conformance)

    def test_channel_images_sit_under_the_users_own(self):
        pol = PlatformPolicy(channels={"default": {}, "pinned": {"pdp": "ghcr.io/o/pdp:1", "vc-apigw": "ghcr.io/o/a:1"}})
        spec = build_spec({"channel": "pinned", "images": {"pdp": "ghcr.io/me/pdp:2"}}, "e", [CAP_CUSTOM_IMAGES], pol)
        self.assertEqual(spec.images, {"pdp": "ghcr.io/me/pdp:2", "vc-apigw": "ghcr.io/o/a:1"})

    def test_a_channel_needs_no_capability(self):
        pol = PlatformPolicy(channels={"default": {}, "pinned": {"pdp": "ghcr.io/o/pdp:1"}})
        self.assertEqual(build_spec({"channel": "pinned"}, "e", policy=pol).images, {"pdp": "ghcr.io/o/pdp:1"})

    def test_a_bad_config_raises_with_every_problem(self):
        with self.assertRaises(PolicyError) as cm:
            build_spec({"trusted_issuers": ["http://x"], "images": {"pdp": "ghcr.io/a/b:1"}}, "e")
        self.assertEqual(sorted(p.path for p in cm.exception.problems), ["images", "trusted_issuers[0]"])

    def test_the_result_is_a_valid_instance_spec_with_known_components(self):
        spec = build_spec({"images": {c: f"ghcr.io/me/{c}:1" for c in component_names()}}, "e", [CAP_CUSTOM_IMAGES])
        spec.validate(component_names())

    def test_raw_values_pass_through_only_with_the_capability(self):
        self.assertEqual(build_spec({"values": {"a": {"b": 1}}}, "e", [CAP_RAW_VALUES]).values, {"a": {"b": 1}})

    def test_build_does_not_mutate_its_input(self):
        cfg = {"trusted_issuers": ["https://i.example.com"], "images": {"pdp": "ghcr.io/me/pdp:1"}}
        import copy
        before = copy.deepcopy(cfg)
        build_spec(cfg, "e", [CAP_CUSTOM_IMAGES])
        self.assertEqual(cfg, before)


if __name__ == "__main__":
    unittest.main()
