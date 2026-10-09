#!/usr/bin/env python3
"""Pre-flight checks must demand exactly what the chosen options use (issue #70), and `make help` must work (issue #69)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stack  # noqa: E402


def names(opts):
    return {c["name"] for c in stack.preflight(opts)}


class Preflight(unittest.TestCase):
    def setUp(self):
        # an empty parent directory: NO sibling checkouts exist, so every checkout check that is asked for fails
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake_root = Path(self.tmp.name) / "sirosid-dev"
        fake_root.mkdir()
        p = mock.patch.object(stack, "SIROSID_DEV_ROOT", fake_root)
        p.start()
        self.addCleanup(p.stop)

    def opts(self, **over):
        return stack.resolve_options("", stack.options_from_make_vars(over))

    def test_without_golden_the_local_checkouts_are_required(self):
        got = names(self.opts())
        self.assertTrue({"wallet-frontend checkout", "go-wallet-backend checkout", "go-trust checkout"} <= got)

    def test_golden_does_not_ask_for_checkouts_it_does_not_use(self):
        for golden in ("yes", "beta_r2"):
            got = names(self.opts(GOLDEN=golden))
            self.assertFalse({"wallet-frontend checkout", "go-wallet-backend checkout", "go-trust checkout"} & got, golden)
            self.assertIn("docker", got)

    def test_golden_still_asks_for_vc_because_vc_always_builds_locally(self):
        got = names(self.opts(GOLDEN="yes", VC="yes"))
        self.assertIn("vc checkout", got)
        self.assertNotIn("wallet-frontend checkout", got)

    def test_the_golden_checklist_is_consistent_with_the_compose_files_it_uses(self):
        """If golden stops resetting a build, its checkout becomes necessary again: tie the two together."""
        opts = self.opts(GOLDEN="yes")
        golden = (ROOT / "docker-compose.golden.yml").read_text()
        for service in ("wallet-frontend", "wallet-backend"):
            self.assertRegex(golden, rf"{service}:\s*\n\s*build: !reset null", service)
        self.assertIn("docker-compose.golden.yml", stack.build_plan("", stack.options_from_make_vars({"GOLDEN": "yes"}), with_checks=False).compose_files)


class MakeHelp(unittest.TestCase):
    def test_make_help_runs_and_documents_registry(self):
        r = subprocess.run(["make", "help"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.strip(), "")
        self.assertIn("REGISTRY=", r.stdout)
        self.assertIn("<vendored|external>", r.stdout)


if __name__ == "__main__":
    unittest.main()
