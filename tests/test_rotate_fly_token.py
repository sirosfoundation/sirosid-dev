"""scripts/rotate_fly_token.py against the fake flyctl: the order of operations, that
the old token is revoked only after the new one is set AND the app is healthy, and
that no token ever appears in the output."""
import importlib.util
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402

_spec = importlib.util.spec_from_file_location("rotate_fly_token", ROOT / "scripts" / "rotate_fly_token.py")
rot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rot)

ORG, APP, NAME = "sirosdev", "sirosid-control-plane", "sirosid-control-plane"
OLD = "FlyV1 fm2_OLDTOKENVALUE"

REAL_ORG_TABLE = (
    'Tokens for organization "sirosdev":\n'
    " ID                                         │ NAME               │ CREATED BY   │ EXPIRES AT                    │ REVOKED AT                    \n"
    " VJO7OyKxQ0lLKIYmkl5GRLkO63Fg2X3QZL39gvIgZ  │ Organization Token │ a@b.se       │ 2126-09-11 08:29:06 +0000 UTC │                               \n"
    " 9XL0L9OjkRAyOiG7nLyk5DnA82HN59Rja05xq8bHZj │ sirosid-env-admin  │ a@b.se       │ 2027-10-03 14:41:15 +0000 UTC │ 2026-10-03 14:43:32 +0000 UTC \n")


class Fake(FakeFly):
    """FakeFly plus failure switches."""

    def __init__(self, fail=()):
        super().__init__()
        self.fail = set(fail)
        self._app(APP)["org"] = ORG
        self.org_tokens = [
            {"ID": "orgtok_old1", "Name": NAME, "org": ORG, "revoked": "", "token": OLD},
            {"ID": "orgtok_gone", "Name": NAME, "org": ORG, "revoked": "2026-09-01 00:00:00 +0000 UTC", "token": "x"},
            {"ID": "orgtok_other", "Name": "Organization Token", "org": ORG, "revoked": "", "token": "y"},
            {"ID": "orgtok_elsewhere", "Name": NAME, "org": "another-org", "revoked": "", "token": "z"},
        ]
        self._app(APP)["secrets"]["FLY_API_TOKEN"] = OLD

    def handle(self, argv):
        key = " ".join(argv[:2])
        if key == "secrets deploy" and "echo" in self.fail:
            # A flyctl error that echoes the value it was given.
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 1, "", "Error: invalid value " + self.apps[APP]["secrets"]["FLY_API_TOKEN"])
        if key in self.fail or (argv[:3] == ["tokens", "create", "org"] and "mint" in self.fail):
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 1, "", "Error: Not authorized to access createlimitedaccesstoken")
        return super().handle(argv)

    def live(self):
        return {t["ID"] for t in self.org_tokens if not t["revoked"]}

    def new_tokens(self):
        return [t for t in self.org_tokens if t["ID"].startswith("orgtok0")]


def rotation(fake, healthy=True, out=None, **kw):
    lines = out if out is not None else []
    fly = FlyClient(org=ORG, token="FlyV1 fm2_ROTATORTOKEN", runner=fake.runner(), out=lines.append, err=lines.append)
    t = [0.0]

    def sleep(s):
        t[0] += s
    health = healthy if callable(healthy) else (lambda url: healthy)
    return rot.Rotation(fly, org=ORG, app=APP, name=NAME, health_url="https://cp.example/healthz", health=health,
                        health_timeout=60, health_interval=5, out=lines.append, sleep=sleep, clock=lambda: t[0], **kw), lines


class ParseTests(unittest.TestCase):
    def test_the_real_table_with_revoked_rows(self):
        rows = rot.parse_tokens(REAL_ORG_TABLE)
        self.assertEqual([r["name"] for r in rows], ["Organization Token", "sirosid-env-admin"])
        self.assertEqual(rows[0]["revoked_at"], "")
        self.assertEqual(rows[1]["revoked_at"], "2026-10-03 14:43:32 +0000 UTC")

    def test_a_table_without_the_revoked_column_is_refused(self):
        with self.assertRaises(ValueError):
            rot.parse_tokens(" ID │ NAME │ CREATED BY │ EXPIRES AT │\n x │ y │ z │ w │\n")

    def test_redact(self):
        self.assertEqual(rot.redact("a FlyV1 fm2_abc,fm2_def b"), "a <redacted> b")
        self.assertEqual(rot.redact("x secretvalue y", "secretvalue"), "x <redacted> y")


class RotationTests(unittest.TestCase):
    def test_happy_path(self):
        fake = Fake()
        r, out = rotation(fake)
        self.assertEqual(r.run(), 0, out)
        new = fake.new_tokens()
        self.assertEqual(len(new), 1)
        self.assertEqual(fake.apps[APP]["secrets"]["FLY_API_TOKEN"], new[0]["token"], "env secret, as-is (not base64)")
        self.assertEqual(fake.live(), {new[0]["ID"], "orgtok_other", "orgtok_elsewhere"},
                         "only our name, only this org, only the ones listed before")
        order = [c for c in fake.log if c.split()[1] in ("tokens", "secrets") and c.split()[2] in ("create", "import", "deploy", "revoke")]
        self.assertEqual([c.split()[2] for c in order], ["create", "import", "deploy", "revoke"])
        self.assertIn("flyctl tokens revoke orgtok_old1", fake.log)
        create = [c for c in fake.log if "tokens create org" in c][0]
        self.assertIn("-x 1440h", create)
        self.assertIn(f"-o {ORG}", create)

    def test_no_token_is_ever_printed(self):
        fake = Fake()
        out = []
        buf_o, buf_e = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_o), redirect_stderr(buf_e):
            r, _ = rotation(fake, out=out)
            self.assertEqual(r.run(), 0)
        text = "\n".join(out) + buf_o.getvalue() + buf_e.getvalue()
        new = fake.new_tokens()[0]["token"]
        for secret in (new, new.split(" ", 1)[1], OLD, "ROTATORTOKEN"):
            self.assertNotIn(secret, text)
        self.assertNotIn("FLY_API_TOKEN=", "\n".join(fake.log), "the value never rides on a command line")

    def test_a_flyctl_error_echoing_the_token_is_redacted(self):
        fake = Fake(fail={"echo"})
        r, out = rotation(fake)
        self.assertEqual(r.run(), 2)
        new = fake.new_tokens()[0]["token"]
        self.assertTrue(any("invalid value" in line for line in out), out)
        self.assertNotIn(new.split(" ", 1)[1], "\n".join(out))

    def test_dry_run_changes_nothing(self):
        fake = Fake()
        before = fake.live()
        r, out = rotation(fake)
        self.assertEqual(r.run(dry_run=True), 0)
        self.assertEqual(fake.live(), before)
        self.assertFalse(any(w in c for c in fake.log for w in ("create", "import", "revoke", "deploy")), fake.log)
        self.assertIn("orgtok_old1", "\n".join(out))

    def test_a_failed_mint_changes_nothing(self):
        fake = Fake(fail={"mint"})
        r, out = rotation(fake)
        self.assertEqual(r.run(), 2)
        self.assertEqual(fake.apps[APP]["secrets"]["FLY_API_TOKEN"], OLD)
        self.assertIn("orgtok_old1", fake.live())
        self.assertIn("Not authorized", "\n".join(out))

    def test_a_failed_secret_set_revokes_the_new_token_and_keeps_the_old(self):
        for step in ("secrets import", "secrets deploy"):
            fake = Fake(fail={step})
            r, _ = rotation(fake)
            self.assertEqual(r.run(), 2, step)
            self.assertIn("orgtok_old1", fake.live(), step)
            self.assertEqual([t["revoked"] != "" for t in fake.new_tokens()], [True], f"{step}: unused new token taken back")

    def test_unhealthy_never_revokes(self):
        fake = Fake()
        r, out = rotation(fake, healthy=False)
        self.assertEqual(r.run(), 3)
        self.assertIn("orgtok_old1", fake.live())
        self.assertFalse(any("tokens revoke" in c for c in fake.log))
        self.assertIn("NOT revoking", "\n".join(out))

    def test_health_must_hold_for_consecutive_checks(self):
        seq = iter([True, False, True, True, True])
        fake = Fake()
        calls = []

        def health(url):
            calls.append(url)
            return next(seq, False)
        r, _ = rotation(fake, healthy=health)
        self.assertEqual(r.run(), 0)
        self.assertEqual(len(calls), 5)

    def test_nothing_old_to_revoke(self):
        fake = Fake()
        fake.org_tokens = [t for t in fake.org_tokens if t["ID"] != "orgtok_old1"]
        r, _ = rotation(fake)
        self.assertEqual(r.run(), 0)
        self.assertFalse(any("tokens revoke" in c for c in fake.log))

    def test_main_needs_the_rotator_token(self):
        import os
        old = os.environ.pop("FLY_ROTATOR_TOKEN", None)
        try:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(rot.main(["--org", ORG, "--app", APP, "--health-url", "https://x/healthz"]), 1)
        finally:
            if old is not None:
                os.environ["FLY_ROTATOR_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
