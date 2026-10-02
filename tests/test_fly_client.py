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


if __name__ == "__main__":
    unittest.main()
