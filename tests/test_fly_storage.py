#!/usr/bin/env python3
"""Pure parts of the Fly storage work: fly.toml generation with a mount, volume
naming, and the generated nginx/dashboard carrying the env-admin wiring.

    python3 -m unittest tests/test_fly_storage.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fly_common  # noqa: E402


class FlyToml(unittest.TestCase):
    def render(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.fly.toml"
            fly_common.write_fly_toml(path, "sirosid-t-mongodb", None, region="arn", **kw)
            return path.read_text()

    def test_mount_block(self):
        text = self.render(mount={"volume": "mongodb_data", "destination": "/data/db"},
                           internal_check={"type": "tcp", "port": 27017})
        self.assertIn("[mounts]\n  source = 'mongodb_data'\n  destination = '/data/db'", text)
        self.assertIn("[checks]", text)

    def test_no_mount_by_default(self):
        self.assertNotIn("[mounts]", self.render())

    def test_volume_names_have_no_hyphens(self):
        for comp in fly_common.STORAGE_APPS:
            name = fly_common.volume_name(comp)
            self.assertRegex(name, r"^[a-z0-9_]+$")
        self.assertEqual(fly_common.volume_name("conformance-mongodb"), "conformance_mongodb_data")

    def test_storage_components_declare_mounts(self):
        by_name = {c["name"]: c for c in fly_common.COMPONENTS + fly_common.CONFORMANCE_COMPONENTS}
        for comp in fly_common.STORAGE_APPS:
            self.assertEqual(by_name[comp]["mount"]["destination"], "/data/db")
            self.assertEqual(by_name[comp]["mount"]["volume"], fly_common.volume_name(comp))

    def test_env_admin_deploys_before_wallet_frontend(self):
        names = [c["name"] for c in fly_common.COMPONENTS]
        self.assertLess(names.index("wallet-backend"), names.index("env-admin"))
        self.assertLess(names.index("env-admin"), names.index("wallet-frontend"))


class MountGuard(unittest.TestCase):
    def test_machine_has_mount_by_name_or_id(self):
        self.assertTrue(fly_common.machine_has_mount({"config": {"mounts": [{"name": "mongodb_data", "path": "/data/db"}]}}, "mongodb_data"))
        self.assertTrue(fly_common.machine_has_mount({"config": {"mounts": [{"volume": "vol_123", "path": "/data/db"}]}}, "mongodb_data"))
        self.assertFalse(fly_common.machine_has_mount({"config": {"mounts": []}}, "mongodb_data"))
        self.assertFalse(fly_common.machine_has_mount({"config": {}}, "mongodb_data"))

    def test_deploy_component_passes_the_mount_and_asserts_it(self):
        # Regression: the first release generated the storage apps' fly.toml
        # without the mount because deploy_component never passed it on. The
        # deploy sequence itself needs flyctl, so pin the two call sites here.
        src = (ROOT / "scripts" / "fly-up.py").read_text()
        self.assertIn('mount=comp.get("mount")', src)
        self.assertIn('assert_volume_mounted(app, comp["mount"]["volume"])', src)

    def test_ensure_running_only_starts_machines_at_rest(self):
        import inspect
        src = inspect.getsource(fly_common.ensure_running)
        self.assertIn('in ("stopped", "suspended")', src)


class GeneratedConfigs(unittest.TestCase):
    def test_nginx_has_env_admin_proxy_and_card(self):
        conf = fly_common.wallet_frontend_conf("t", conformance=False)
        self.assertIn("location /_admin/", conf)
        self.assertIn("sirosid-t-env-admin.internal:3002", conf)
        self.assertIn("location = /storage-card.js", conf)
        self.assertIn("/_health/env-admin", conf)

    def test_dashboard_has_storage_card(self):
        html = fly_common.wallet_frontend_dashboard_html("t")
        self.assertIn('id="storage-card"', html)
        self.assertIn('<script src="/storage-card.js">', html)
        self.assertIn('"env-admin"', html)

    def test_local_and_fly_dashboards_share_the_card_file(self):
        local = (ROOT / "startup.html").read_text()
        self.assertIn('id="storage-card"', local)
        self.assertIn('<script src="/storage-card.js">', local)
        self.assertTrue((ROOT / "dashboard" / "storage-card.js").is_file())


if __name__ == "__main__":
    unittest.main()
