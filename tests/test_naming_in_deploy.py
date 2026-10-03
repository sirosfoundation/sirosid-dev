"""A deploy under a different naming scheme must not leak the default one.

The hosted service gives every instance a generated app prefix and a hostname on
a domain it owns. Anything in a rendered config that still spells
`sirosid-<env>-...` or `...fly.dev` would send a wallet or a browser to the wrong
place - or, worse, to somebody else's instance. This runs the real fly-up against
the fake flyctl (tests/fakefly.py) under a non-default scheme and scans every
file it renders and every command it issues.

Comment lines are exempt: the generated nginx configs explain themselves in
comments that mention fly.dev, which is documentation, not routing.
"""
import re
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakefly  # noqa: E402

PREFIX = "sid"
PATTERN = "{env}-{component}.sandbox.example"
OLD_MARKERS = ("sirosid-coretest", ".fly.dev")


def _leaks_in(line):
    return [m for m in OLD_MARKERS if m in line]
SKIP = re.compile(r"vc-pki|\.pem$|\.png$|\.crt$|\.key$")


def _meaningful_lines(text, path):
    for line in text.split("\n"):
        if path.endswith(".conf") and line.lstrip().startswith("#"):
            continue
        yield line


@unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
class NamingInDeploy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = fakefly.ROOT / "fixtures" / "rendered" / "fly-coretest"
        cls.result = fakefly.run_fly_up(["--app-prefix", PREFIX, "--host-pattern", PATTERN], keep_state=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_deploy_succeeds(self):
        self.assertEqual(self.result.exit, 0, getattr(self.result, "error", ""))

    def test_no_rendered_file_mentions_the_default_scheme(self):
        leaks = []
        for p in sorted(self.out.rglob("*")):
            rel = p.relative_to(self.out).as_posix()
            if not p.is_file() or SKIP.search(rel):
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            for line in _meaningful_lines(text, rel):
                leaks += [f"{rel}: {m} in {line.strip()[:120]!r}" for m in _leaks_in(line)]
        self.assertEqual(leaks, [])

    def test_no_command_mentions_the_default_scheme(self):
        leaks = [c[:160] for c in self.result.commands if _leaks_in(c)]
        self.assertEqual(leaks, [])

    def test_apps_use_the_prefix(self):
        created = [c.split()[3] for c in self.result.commands if " apps create " in c]
        self.assertTrue(created)
        self.assertTrue(all(a.startswith(f"{PREFIX}-coretest-") for a in created), created)

    def test_public_identity_follows_the_pattern(self):
        import yaml
        backend = yaml.safe_load((self.out / "wallet-backend.yaml").read_text())
        self.assertEqual(backend["server"]["rp_id"], "coretest-wallet-frontend.sandbox.example")
        self.assertEqual(backend["trust"]["pdp_url"], f"http://{PREFIX}-coretest-pdp.internal:8080")
        apigw = (self.out / "vc-apigw.yaml").read_text()
        self.assertIn("coretest-vc-apigw.sandbox.example", apigw)


if __name__ == "__main__":
    unittest.main()
