"""env-admin is optional: a hosted service has no credential to give it."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from sirosid_core.assets import wallet_frontend_conf, wallet_frontend_dashboard_html  # noqa: E402
from sirosid_core.components import CONFORMANCE_COMPONENTS, build_components  # noqa: E402
from sirosid_core.deploy import deploy_instance, deploy_order  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.policy import PlatformPolicy, build_spec  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_core.spec import InstanceSpec  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
N = Naming("abc", app_prefix="sid")


class GeneratedFilesTests(unittest.TestCase):
    def test_on_by_default_everything_is_there(self):
        conf = wallet_frontend_conf("abc", False, N)
        self.assertIn("location /_admin/", conf)
        self.assertIn("/_health/env-admin", conf)
        self.assertIn("storage-card.js", conf)
        html = wallet_frontend_dashboard_html("abc", naming=N)
        self.assertIn('id="storage-card"', html)
        self.assertIn('<script src="/storage-card.js">', html)
        self.assertIn('"env-admin"', html)

    def test_off_nothing_refers_to_env_admin(self):
        conf = wallet_frontend_conf("abc", False, N, env_admin=False)
        self.assertNotIn("env-admin", conf)
        self.assertNotIn("/_admin/", conf)
        self.assertNotIn("storage-card", conf)
        html = wallet_frontend_dashboard_html("abc", naming=N, env_admin=False)
        for needle in ("env-admin", "storage-card", "Checking env-admin"):
            self.assertNotIn(needle, html)

    def test_off_still_a_working_config(self):
        conf = wallet_frontend_conf("abc", True, N, env_admin=False)          # with conformance too
        self.assertIn("location = /_health/vc-apigw", conf)
        self.assertEqual(conf.count("{"), conf.count("}"), "braces balance")


class OrderTests(unittest.TestCase):
    COMPS = build_components("m", "e")

    def names(self, **kw):
        return [c["name"] for c in deploy_order(self.COMPS, **kw)]

    def test_default_includes_env_admin(self):
        self.assertIn("env-admin", self.names(conformance=False))

    def test_dropped_when_off_and_nothing_else_moves(self):
        on = self.names(conformance=False)
        off = self.names(conformance=False, env_admin=False)
        self.assertEqual(off, [n for n in on if n != "env-admin"])

    def test_with_conformance(self):
        off = self.names(conformance=True, env_admin=False)
        self.assertNotIn("env-admin", off)
        self.assertLess(off.index("conformance-server"), off.index("wallet-frontend"))
        self.assertEqual(off[-1], "conformance")


class SpecAndPolicyTests(unittest.TestCase):
    def test_spec_round_trips_the_flag(self):
        s = InstanceSpec(env="x", env_admin=False)
        self.assertFalse(InstanceSpec.from_dict(s.to_dict()).env_admin)
        self.assertTrue(InstanceSpec(env="x").env_admin)

    def test_the_platform_decides_a_user_cannot(self):
        self.assertFalse(build_spec({}, "e", policy=PlatformPolicy(env_admin=False)).env_admin)
        self.assertTrue(build_spec({}, "e", policy=PlatformPolicy()).env_admin)
        from sirosid_core.policy import validate
        self.assertTrue(validate({"env_admin": False}), "not a key a user may set")


@NEEDS
class DeployTests(unittest.TestCase):
    def deploy(self, env_admin):
        fake = FakeFly()
        fly = FlyClient(org="sandbox", runner=fake.runner(), docker=lambda c, **k: subprocess.CompletedProcess(c, 0),
                        out=lambda m: None, err=lambda m: None)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        spec = InstanceSpec(env="abc", region="arn", app_prefix="sid", env_admin=env_admin)
        result = deploy_instance(spec, fly, N, Resources(ROOT), rendered_root=root, progress=None)
        return fake, result, root / "fly-abc"

    def test_without_env_admin_no_app_no_token_no_card_upload(self):
        fake, result, out = self.deploy(False)
        self.assertNotIn("sid-abc-env-admin", fake.apps)
        self.assertNotIn("env-admin", result.deployed)
        self.assertFalse([c for c in fake.log if c.startswith("flyctl tokens create")],
                         "nothing may need a limited-access token: an org credential cannot mint one")
        self.assertFalse([c for c in fake.log if "storage-card.js" in c])
        self.assertNotIn("env-admin", (out / "wallet-frontend.conf").read_text())
        self.assertIn("wallet-frontend", result.deployed)

    def test_with_env_admin_as_before(self):
        fake, result, _ = self.deploy(True)
        self.assertIn("sid-abc-env-admin", fake.apps)
        self.assertTrue([c for c in fake.log if c.startswith("flyctl tokens create")])
        self.assertTrue([c for c in fake.log if "storage-card.js" in c])

    def test_stop_start_destroy_do_not_need_it(self):
        from sirosid_core.lifecycle import destroy_instance, start_instance, stop_instance
        fake, _, _ = self.deploy(False)
        fly = FlyClient(org="sandbox", runner=fake.runner(), out=lambda m: None, err=lambda m: None)
        self.assertTrue(stop_instance(fly, N).ok)
        self.assertTrue(start_instance(fly, N).ok)
        rep = destroy_instance(fly, N)
        self.assertTrue(rep.ok)
        self.assertEqual(fake.apps, {})


@NEEDS
class ServiceTests(unittest.TestCase):
    def test_the_control_plane_deploys_without_env_admin_by_default(self):
        from test_service import admin_and_user, make
        cp, fake, _ = make()
        self.assertFalse(cp.platform.env_admin)
        _, alice = admin_and_user(cp)
        inst = cp.create_instance(alice)
        self.assertEqual(inst["status"], "running")
        self.assertFalse([a for a in fake.apps if a.endswith("-env-admin")])
        self.assertFalse([c for c in fake.log if c.startswith("flyctl tokens create")])


if __name__ == "__main__":
    unittest.main()
