#!/usr/bin/env python3
"""scripts/stack.py must agree with the Makefile about COMPOSE_FILES.

The Makefile still builds the list itself (scripts/stack.py is read-only for
now - see its module doc). This test runs both for every option combination
that matters and fails on the first disagreement, so a new overlay added to
one side and not the other is caught here rather than by a developer whose
TUI-started stack quietly differs from their `make up`.

    python3 -m unittest tests/test_stack_parity.py
"""
import itertools
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import stack  # noqa: E402


def make_compose_files(assignments: dict) -> list:
    args = [f"{k}={v}" for k, v in assignments.items()]
    out = subprocess.run(["make", "-s", "_print-compose-files", *args], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return [tok for tok in out if tok != "-f"]


class ComposeFileParity(unittest.TestCase):
    def check(self, assignments: dict):
        expected = make_compose_files(assignments)
        got = stack.build_plan("", stack.options_from_make_vars(assignments), with_checks=False).compose_files
        self.assertEqual(got, expected, f"for {assignments}")

    def test_defaults(self):
        self.check({})

    def test_every_pdp_mode(self):
        for pdp in ("allow", "whitelist", "deny", "mock", "helm"):
            self.check({"PDP": pdp})

    def test_pdp_with_vc(self):
        for pdp in ("allow", "helm", "mock"):
            self.check({"PDP": pdp, "VC": "yes"})

    def test_single_flags(self):
        for flag in ("VC", "CONFORMANCE", "R2PS", "TUNNELS", "FACETEC"):
            self.check({flag: "yes"})
        self.check({"AS_RULES": "baseline"})
        self.check({"DOMAIN": "myhost.local"})
        self.check({"TRANSPORT": "wmp"})
        self.check({"TRANSPORT": "http"})
        self.check({"GOLDEN": "yes"})
        self.check({"GOLDEN": "beta_r2", "PDP": "mock"})

    def test_combinations(self):
        flags = ("VC", "CONFORMANCE", "R2PS", "FACETEC")
        for pdp in ("allow", "helm"):
            for n in range(1, len(flags) + 1):
                for combo in itertools.combinations(flags, n):
                    self.check({"PDP": pdp, **{f: "yes" for f in combo}})

    def test_golden_with_vc(self):
        self.check({"GOLDEN": "yes", "VC": "yes"})
        self.check({"GOLDEN": "yes", "VC": "yes", "CONFORMANCE": "yes", "TRANSPORT": "wmp"})


class OptionSchema(unittest.TestCase):
    def test_rejects_unknown_enum_value(self):
        with self.assertRaises(ValueError):
            stack.options_from_make_vars({"PDP": "nope"})

    def test_tunnels_and_domain_conflict_is_an_error(self):
        plan = stack.build_plan("", {"tunnels": True, "domain": "x.local"}, with_checks=False)
        self.assertTrue(plan.errors)

    def test_stores_follow_pdp_mode(self):
        memory = stack.build_plan("", {}, with_checks=False).stores[0]
        mongo = stack.build_plan("", {"pdp": "helm"}, with_checks=False).stores[0]
        self.assertEqual(memory["kind"], "memory")
        self.assertEqual(mongo["kind"], "mongodb")
        self.assertTrue(mongo["persistent"])

    def test_make_args_round_trip(self):
        opts = stack.resolve_options("", {"pdp": "helm", "vc": True, "transport": "wmp"})
        again = stack.options_from_make_vars(dict(a.split("=", 1) for a in stack.make_vars(opts)))
        self.assertEqual(stack.resolve_options("", again), opts)


if __name__ == "__main__":
    unittest.main()
