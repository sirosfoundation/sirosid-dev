import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from sirosid_core.android import apk_key_hash_to_hex, hex_to_apk_key_hash, identities_from_entries, parse_identity  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402


class ResourcesTests(unittest.TestCase):
    def test_paths_hang_off_the_root(self):
        r = Resources(ROOT)
        self.assertEqual(r.chart_dir, ROOT / "chart")
        self.assertEqual(r.values_base, ROOT / "values-base.yaml")
        self.assertEqual(r.rendered, ROOT / "fixtures" / "rendered")

    def test_this_repo_is_complete(self):
        self.assertEqual(Resources(ROOT).missing(), [])

    def test_an_empty_directory_reports_what_is_missing(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.assertEqual(sorted(Resources(d).missing()), ["chart/", "fixtures/", "values-base.yaml", "values-fly.yaml"])

    def test_render_refuses_to_guess_where_the_repo_is(self):
        from sirosid_core.render import render
        with self.assertRaises(TypeError):
            render("compose", ROOT / "chart")


class AndroidIdentityTests(unittest.TestCase):
    HEX = "AA:BB:CC:DD:EE:FF:00:11"

    def test_hex_and_base64url_round_trip(self):
        self.assertEqual(apk_key_hash_to_hex(hex_to_apk_key_hash(self.HEX)), self.HEX)

    def test_either_encoding_is_accepted(self):
        a = parse_identity("org.x", self.HEX)
        b = parse_identity("org.x", a["apk_key_hash"])
        self.assertEqual(a, b)

    def test_entries_are_split_deduplicated_and_ordered(self):
        got = identities_from_entries([f"org.x={self.HEX},org.y={self.HEX}", f"org.x={self.HEX}", ""])
        self.assertEqual([i["package"] for i in got], ["org.x", "org.y"])

    def test_malformed_entry_is_a_value_error_not_an_exit(self):
        with self.assertRaises(ValueError):
            identities_from_entries(["no-equals-sign"])

    def test_cli_wrapper_still_turns_it_into_a_usage_error(self):
        import android_apps
        with self.assertRaises(SystemExit):
            android_apps.load_android_apps(extra=["no-equals-sign"], root=Path(tempfile.mkdtemp()))


class ShimTests(unittest.TestCase):
    def test_moved_modules_resolve_to_the_package_module(self):
        import api_auth, helm_render_lib, vc_render
        from sirosid_core import api_auth as a, helm as h, vc_render as v
        self.assertIs(api_auth, a)
        self.assertIs(helm_render_lib, h)
        self.assertIs(vc_render, v)


if __name__ == "__main__":
    unittest.main()
