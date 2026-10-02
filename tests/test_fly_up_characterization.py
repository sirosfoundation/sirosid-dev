"""Characterization of `make fly-up` against a fake flyctl.

Runs the real scripts/fly-up.py end to end (see tests/fakefly.py) and compares
the exact flyctl/docker command sequence and a hash of every deterministic
rendered file with tests/golden/fly_up.json. Nothing here talks to Fly.

It exists so the deploy path can be refactored (the core-library extraction for
the self-service service) with proof that behaviour did not change. A failure
means one of two things, and you have to decide which:

  * you changed behaviour by accident - fix the code;
  * you changed it on purpose (a chart edit, a new component, a new flag) -
    read the diff, then refresh with   UPDATE_GOLDEN=1 python3 -m unittest
    tests.test_fly_up_characterization   and commit the golden with the change.

Needs `helm` and `openssl` (both run for real). Skipped without them.
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakefly  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden" / "fly_up.json"
FULL = (Path(__file__).resolve().parent / "scenarios" / "full.yaml").read_text()


def scenarios():
    out = {}
    out["default"] = fakefly.run_fly_up([])
    out["full-environment-file"] = fakefly.run_fly_up([], env_yaml=FULL)
    out["conformance"] = fakefly.run_fly_up(["--conformance"])
    out["wallet-attestation"] = fakefly.run_fly_up(["--wallet-attestation"])
    out["with-trust-flags"] = fakefly.run_fly_up(
        ["--trusted-issuer", "https://issuer.example.test",
         "--trusted-verifier", "x509_hash:AAAA",
         "--images", "pdp=ghcr.io/sirosfoundation/go-trust:9.9.9"])
    summary = {k: {"exit": v.exit, "commands": v.commands, "files": v.files} for k, v in out.items()}
    # Idempotent redeploy: the SECOND run against one fake Fly. What it issues
    # is the contract that redeploys never recreate apps or volumes.
    fake = fakefly.FakeFly()
    first = fakefly.run_fly_up([], fake=fake, keep_state=True)
    second = fakefly.run_fly_up([], fake=fake, keep_state=True)
    shutil.rmtree(fakefly.ROOT / "fixtures" / "rendered" / "fly-coretest", ignore_errors=True)
    summary["redeploy-second-run"] = {"exit": second.exit, "commands": second.commands[len(first.commands):],
                                      "files": second.files}
    return summary


@unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
class FlyUpCharacterization(unittest.TestCase):
    maxDiff = None

    def test_matches_golden(self):
        got = scenarios()
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
            self.skipTest(f"golden rewritten: {GOLDEN}")
        want = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(got), sorted(want), "scenario set changed")
        for name in sorted(want):
            with self.subTest(scenario=name):
                self.assertEqual(got[name]["exit"], want[name]["exit"])
                self.assertEqual(got[name]["commands"], want[name]["commands"], "flyctl/docker command sequence")
                self.assertEqual(got[name]["files"], want[name]["files"], "rendered files")

    def test_redeploy_creates_nothing(self):
        redeploy = scenarios()["redeploy-second-run"]
        created = [c for c in redeploy["commands"] if " apps create " in c or " volumes create " in c]
        self.assertEqual(created, [], "an idempotent redeploy must not create apps or volumes")
        self.assertEqual(redeploy["exit"], 0)

    def test_every_scenario_succeeds(self):
        for name, r in scenarios().items():
            with self.subTest(scenario=name):
                self.assertEqual(r["exit"], 0)


if __name__ == "__main__":
    unittest.main()
