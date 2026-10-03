import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from sirosid_core.fly import FlyClient, FlyError  # noqa: E402


def client(fake=None, **kw):
    fake = fake or FakeFly()
    return FlyClient(runner=fake.runner(), out=lambda m: None, err=lambda m: None, **kw), fake


class FlyClientTests(unittest.TestCase):
    def test_creates_apps_in_its_own_org_not_the_default(self):
        c, fake = client(org="sandbox-org")
        c.ensure_app("sid-a-pdp", network="sid-a")
        create = next(x for x in fake.log if " apps create " in x)
        self.assertIn("-o sandbox-org", create)
        self.assertNotIn("sirosfoundation", create)
        self.assertIn("--network sid-a", create)

    def test_ensure_app_is_idempotent(self):
        c, fake = client()
        c.ensure_app("x")
        c.ensure_app("x")
        self.assertEqual(sum(" apps create " in x for x in fake.log), 1)

    def test_every_call_carries_the_token_it_was_given(self):
        c, fake = client(token="FlyV1 org-scoped")
        c.ensure_app("x")
        c.list_machines("x")
        self.assertTrue(fake.tokens_seen)
        self.assertEqual(set(fake.tokens_seen), {"FlyV1 org-scoped"})

    def test_no_token_means_ambient_login(self):
        c, fake = client()
        c.ensure_app("x")
        self.assertEqual(set(fake.tokens_seen), {None})

    def test_failure_is_an_exception_not_a_system_exit(self):
        c, _ = client()
        with self.assertRaises(FlyError):
            c.run("nonsense", "subcommand")          # the fake rejects it with exit 1

    def test_failure_can_be_tolerated_with_check_false(self):
        c, _ = client()
        self.assertNotEqual(c.run("nonsense", check=False).returncode, 0)

    def test_volume_in_another_region_is_refused(self):
        c, _ = client()
        c.ensure_app("x")
        c.ensure_volume("x", "data", "arn")
        with self.assertRaises(FlyError) as cm:
            c.ensure_volume("x", "data", "fra")
        self.assertIn("pins its machine", str(cm.exception))

    def test_volume_is_created_once(self):
        c, fake = client()
        c.ensure_app("x")
        self.assertTrue(c.ensure_volume("x", "data", "arn")["created"])
        self.assertFalse(c.ensure_volume("x", "data", "arn")["created"])
        self.assertEqual(sum("volumes create" in x for x in fake.log), 1)

    def test_secret_is_never_rotated_unless_forced(self):
        c, fake = client()
        c.ensure_app("x")
        c.ensure_secret("x", "k", "one")
        c.ensure_secret("x", "k", "two")
        self.assertEqual(sum("secrets set" in x for x in fake.log), 1)
        c.ensure_secret("x", "k", "three", force=True)
        self.assertEqual(sum("secrets set" in x for x in fake.log), 2)

    def test_docker_auth_happens_once_per_client_not_once_per_process(self):
        import subprocess
        ok = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0)

        def make():
            fake = FakeFly()
            return FlyClient(runner=fake.runner(), docker=ok, out=lambda m: None, err=lambda m: None), fake

        (a, fa), (b, fb) = make(), make()
        for c in (a, b):
            c.ensure_app("x")
        a.push_local_image("x", "img:1")
        a.push_local_image("x", "img:2")
        b.push_local_image("x", "img:1")
        self.assertEqual(sum("auth docker" in x for x in fa.log), 1, "a authenticates once for two pushes")
        self.assertEqual(sum("auth docker" in x for x in fb.log), 1, "b authenticates for itself; no shared global")

    def test_default_client_uses_the_default_org(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import fly_common
        self.assertEqual(fly_common._client.org, fly_common.FLY_ORG)

    def test_script_facade_turns_flyerror_into_system_exit(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import fly_common
        fake = FakeFly()
        fly_common._client._runner = fake.runner()
        fly_common._client._err = lambda m: None
        try:
            with self.assertRaises(SystemExit):
                fly_common.run_fly("nonsense")
        finally:
            fly_common._client._runner = lambda *a, **k: __import__("subprocess").run(*a, **k)


class SlowChecks(FakeFly):
    """A flyctl whose health checks never turn healthy."""

    def handle(self, argv):
        if argv[:2] == ["checks", "list"]:
            self.log.append("flyctl " + " ".join(argv))
            import json
            return __import__("subprocess").CompletedProcess(
                argv, 0, json.dumps({"m": [{"name": "internal", "status": "critical"}]}), "")
        return super().handle(argv)


class WaitForChecksTests(unittest.TestCase):
    def test_a_check_that_never_passes_warns_and_carries_on(self):
        """The path a real, slow deploy takes. Found only on real Fly: the warning
        used to raise TypeError because a multi-line print kept its file= keyword."""
        warned = []
        fake = SlowChecks()
        c = FlyClient(runner=fake.runner(), out=lambda m: None, err=warned.append)
        c.ensure_app("x")
        c.wait_for_checks("x", timeout=0.05, poll_interval=0.01)         # must not raise
        self.assertTrue(any("did not report passing" in w for w in warned), warned)

    def test_passing_checks_return_quietly(self):
        said = []
        fake = FakeFly()
        c = FlyClient(runner=fake.runner(), out=said.append, err=lambda m: None)
        c.ensure_app("x")
        c.run("deploy", "-a", "x")
        c.wait_for_checks("x", timeout=1, poll_interval=0.01)
        self.assertTrue(any("all checks passing" in m for m in said))


class SettlingMachine(FakeFly):
    """A machine that is 'replacing' for the first few looks and then rests as `final`
    - what Fly reports right after a deploy of a stopped machine."""

    def __init__(self, transitional_looks, final="stopped"):
        super().__init__()
        self.looks, self.final = transitional_looks, final

    def handle(self, argv):
        out = super().handle(argv)
        if argv[:2] == ["machine", "list"]:
            import json
            machines = json.loads(out.stdout)
            for m in machines:
                if self.looks > 0:
                    m["state"] = "replacing"
                else:
                    m["state"] = self.final
            self.looks -= 1
            out.stdout = json.dumps(machines)
        return out


class EnsureRunningTests(unittest.TestCase):
    def client(self, fake):
        return FlyClient(runner=fake.runner(), out=lambda m: None, err=lambda m: None)

    def starts(self, fake):
        return [x for x in fake.log if x.startswith("flyctl machine start")]

    def test_waits_for_a_machine_to_settle_then_starts_it(self):
        """The real-Fly bug: judged mid-transition, a kept Mongo was never started."""
        fake = SettlingMachine(transitional_looks=3)
        c = self.client(fake)
        c.ensure_app("x")
        c.run("deploy", "-a", "x")
        c.ensure_running("x", settle_timeout=5, poll_interval=0.01)
        self.assertEqual(len(self.starts(fake)), 1)

    def test_a_machine_already_at_rest_is_judged_on_the_first_look(self):
        fake = SettlingMachine(transitional_looks=0)
        c = self.client(fake)
        c.ensure_app("x")
        c.run("deploy", "-a", "x")
        n = sum("machine list" in x for x in fake.log)
        c.ensure_running("x", settle_timeout=5, poll_interval=0.01)
        self.assertEqual(sum("machine list" in x for x in fake.log) - n, 1, "no extra polling when nothing is moving")
        self.assertEqual(len(self.starts(fake)), 1)

    def test_a_running_machine_is_left_alone(self):
        fake = SettlingMachine(transitional_looks=2, final="started")
        c = self.client(fake)
        c.ensure_app("x")
        c.run("deploy", "-a", "x")
        c.ensure_running("x", settle_timeout=5, poll_interval=0.01)
        self.assertEqual(self.starts(fake), [])

    def test_a_machine_that_never_settles_does_not_hang_the_deploy(self):
        fake = SettlingMachine(transitional_looks=10**6)
        c = self.client(fake)
        c.ensure_app("x")
        c.run("deploy", "-a", "x")
        c.ensure_running("x", settle_timeout=0.05, poll_interval=0.01)       # returns
        self.assertEqual(self.starts(fake), [], "and does not issue a start it knows would fail")


class HelmFailureTests(unittest.TestCase):
    def test_a_helm_failure_is_an_exception_with_helms_message_not_an_exit(self):
        from sirosid_core.helm import HelmError, helm_template
        with self.assertRaises(HelmError) as cm:
            helm_template(ROOT / "no-such-chart", [], "ns")
        self.assertIn("helm template failed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class OrgScopingTests(unittest.TestCase):
    def test_list_apps_sees_only_its_own_org(self):
        """Found on real Fly: without -o, list_apps returned every app the login could
        see. A sweeper that destroys apps it does not know must never be able to reach
        another org's."""
        fake = FakeFly()
        sandbox = FlyClient(org="sandbox", runner=fake.runner(), out=lambda m: None, err=lambda m: None)
        prod = FlyClient(org="prod", runner=fake.runner(), out=lambda m: None, err=lambda m: None)
        sandbox.ensure_app("sid-a-pdp")
        prod.ensure_app("sirosid-gdc-pdp")
        self.assertEqual(sandbox.list_apps(), ["sid-a-pdp"])
        self.assertEqual(prod.list_apps(), ["sirosid-gdc-pdp"])

    def test_a_failure_carries_flyctls_own_explanation(self):
        import subprocess
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "Error: Not authorized to access this createlimitedaccesstoken\n")
        c = FlyClient(runner=runner, out=lambda m: None, err=lambda m: None)
        with self.assertRaises(FlyError) as cm:
            c.run("tokens", "create", "deploy", capture=True)
        self.assertIn("Not authorized", str(cm.exception))


REAL_TABLE = """Tokens for application "sid-a-pdp":
 ID                                         │ NAME              │ CREATED BY   │ EXPIRES AT                    │
 qgBZBk3bXmy53t5M1mpnOP1X6YHpN8NY45xy0mOTYV │ sirosid-core-test │ leifj@mnt.se │ 2026-10-02 22:57:58 +0000 UTC │
 60XMXnbzk4eybuLoxgj02GxemnC7eBeXlvbZJKwtw7 │ sirosid-env-admin │ leifj@mnt.se │ 2027-10-02 20:56:16 +0000 UTC │
"""


class TokenRevocationTests(unittest.TestCase):
    def test_parses_the_real_flyctl_table(self):
        rows = FlyClient.parse_token_table(REAL_TABLE)
        self.assertEqual([r["name"] for r in rows], ["sirosid-core-test", "sirosid-env-admin"])
        self.assertTrue(rows[1]["id"].startswith("60XMXnb"))

    def test_revoke_actually_revokes_and_only_the_named_tokens(self):
        """Regression: it asked for --json, which tokens list does not have."""
        fake = FakeFly()
        c = FlyClient(runner=fake.runner(), out=lambda m: None, err=lambda m: None)
        c.ensure_app("x")
        for name in ("sirosid-env-admin", "sirosid-env-admin", "something-else"):
            c.run("tokens", "create", "deploy", "-a", "x", "--name", name, "--json", capture=True)
        self.assertEqual(c.revoke_tokens("x"), 2)
        left = [t["Name"] for t in fake.apps["x"]["tokens"]]
        self.assertEqual(left, ["something-else"])
