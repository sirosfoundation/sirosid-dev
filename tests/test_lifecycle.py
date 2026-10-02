import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from sirosid_core.components import STORAGE_APPS, component_names  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.lifecycle import destroy_instance, start_instance, stop_instance  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402


class FlakyFly(FakeFly):
    """A flyctl whose `apps destroy` fails for the named apps."""

    def __init__(self, fail=()):
        super().__init__()
        self.fail = set(fail)

    def handle(self, argv):
        if argv[:2] == ["apps", "destroy"] and argv[2] in self.fail:
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        return super().handle(argv)


def setup(naming, components=None, fake=None, org="sandbox"):
    fake = fake or FakeFly()
    fly = FlyClient(org=org, runner=fake.runner(), out=lambda m: None, err=lambda m: None)
    for name in components or component_names(include_conformance=False):
        fly.ensure_app(naming.app(name), network=naming.network())
    return fly, fake


class DestroyInstanceTests(unittest.TestCase):
    def test_destroys_every_app_of_the_instance(self):
        n = Naming("abc", app_prefix="sid")
        fly, fake = setup(n)
        report = destroy_instance(fly, n)
        self.assertTrue(report.ok)
        self.assertEqual(sorted(report.destroyed), sorted(n.app(c) for c in component_names(False)))
        self.assertEqual(fake.apps, {})
        # The conformance apps were never there: attempted, nothing to do.
        self.assertTrue(all("conformance" in a for a in report.absent))

    def test_only_touches_its_own_instance(self):
        mine, other = Naming("mine", app_prefix="sid"), Naming("other", app_prefix="sid")
        fake = FakeFly()
        fly, _ = setup(mine, fake=fake)
        setup(other, fake=fake)
        destroy_instance(fly, mine)
        self.assertEqual(sorted(fake.apps), sorted(other.app(c) for c in component_names(False)))

    def test_prefix_matters(self):
        n = Naming("abc", app_prefix="sid")
        fly, fake = setup(n)
        destroy_instance(fly, Naming("abc"))                 # default prefix: wrong instance
        self.assertEqual(len(fake.apps), len(component_names(False)), "must not destroy a differently-prefixed instance")

    def test_keep_data_leaves_storage_apps_stopped(self):
        n = Naming("abc")
        fake = FakeFly()
        fly, _ = setup(n, fake=fake)
        for c in STORAGE_APPS:
            if n.app(c) in fake.apps:
                fly.run("deploy", "-a", n.app(c))              # give it a machine to stop
        report = destroy_instance(fly, n, keep_data=True)
        self.assertEqual([a for a in report.kept], [n.app("mongodb")])
        self.assertEqual(list(fake.apps), [n.app("mongodb")])
        self.assertTrue(all(m["state"] == "stopped" for m in fake.apps[n.app("mongodb")]["machines"]))

    def test_one_failure_does_not_stop_the_rest(self):
        n = Naming("abc")
        fake = FlakyFly(fail={n.app("pdp")})
        fly, _ = setup(n, fake=fake)
        report = destroy_instance(fly, n)
        self.assertFalse(report.ok)
        self.assertEqual([a for a, _ in report.failed], [n.app("pdp")])
        self.assertEqual(list(fake.apps), [n.app("pdp")], "everything else is gone, only the failed app remains")
        self.assertEqual(len(report.destroyed), len(component_names(False)) - 1)

    def test_progress_is_reported_through_the_callback(self):
        n = Naming("abc")
        fly, _ = setup(n)
        seen = []
        destroy_instance(fly, n, progress=seen.append)
        self.assertTrue(any("destroying" in m for m in seen))

    def test_runs_in_the_clients_org(self):
        n = Naming("abc")
        fly, fake = setup(n, org="sandbox")
        self.assertTrue(all("-o sandbox" in x for x in fake.log if " apps create " in x))


class ListAppsTests(unittest.TestCase):
    def test_lists_apps_in_the_org(self):
        n = Naming("abc", app_prefix="sid")
        fly, _ = setup(n, components=["pdp", "mongodb"])
        self.assertEqual(sorted(fly.list_apps()), sorted([n.app("pdp"), n.app("mongodb")]))


def running(naming, fake=None):
    """An instance with a started machine in each app."""
    fake = fake or FakeFly()
    fly, _ = setup(naming, fake=fake)
    for name in component_names(include_conformance=False):
        fly.run("deploy", "-a", naming.app(name))
    return fly, fake


class ScaleToZeroTests(unittest.TestCase):
    N = Naming("abc", app_prefix="sid")

    def states(self, fake):
        return {a: {m["state"] for m in v["machines"]} for a, v in fake.apps.items()}

    def test_stop_stops_every_machine_and_keeps_every_app(self):
        fly, fake = running(self.N)
        report = stop_instance(fly, self.N)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.changed), len(component_names(False)))
        self.assertEqual(len(fake.apps), len(component_names(False)), "apps (and so volumes) are kept")
        self.assertTrue(all(st == {"stopped"} for st in self.states(fake).values()))

    def test_stop_goes_consumers_first_and_mongo_last(self):
        fly, fake = running(self.N)
        stop_instance(fly, self.N)
        stops = [c.split()[4] for c in fake.log if c.startswith("flyctl machine stop")]
        apps = [c.split()[-1] for c in fake.log if c.startswith("flyctl machine stop")]
        order = [a.replace("sid-abc-", "") for a in apps]
        self.assertEqual(order[-1], "mongodb")
        self.assertLess(order.index("wallet-frontend"), order.index("wallet-backend"))

    def test_start_brings_it_back_in_deploy_order_mongo_first(self):
        fly, fake = running(self.N)
        stop_instance(fly, self.N)
        n = len(fake.log)
        report = start_instance(fly, self.N)
        self.assertTrue(report.ok)
        self.assertTrue(all(st == {"started"} for st in self.states(fake).values()))
        starts = [c.split()[-1] for c in fake.log[n:] if c.startswith("flyctl machine start")]
        order = [a.replace("sid-abc-", "") for a in starts]
        self.assertEqual(order[0], "mongodb")
        self.assertLess(order.index("mongodb"), order.index("vc-registry"))
        self.assertLess(order.index("vc-issuer"), order.index("vc-verifier"))

    def test_start_waits_for_health_only_where_there_is_a_check(self):
        fly, fake = running(self.N)
        stop_instance(fly, self.N)
        n = len(fake.log)
        start_instance(fly, self.N)
        waited = {c.split()[4] for c in fake.log[n:] if c.startswith("flyctl checks list")}
        self.assertIn("sid-abc-mongodb", waited)
        self.assertIn("sid-abc-wallet-backend", waited)

    def test_start_on_a_running_instance_starts_nothing(self):
        fly, fake = running(self.N)
        n = len(fake.log)
        start_instance(fly, self.N)
        self.assertFalse([c for c in fake.log[n:] if c.startswith("flyctl machine start")])

    def test_start_repairs_a_half_awake_instance(self):
        """What a stray request leaves behind when machines can wake themselves."""
        fly, fake = running(self.N)
        stop_instance(fly, self.N)
        fake.apps["sid-abc-wallet-frontend"]["machines"][0]["state"] = "started"
        report = start_instance(fly, self.N)
        self.assertTrue(report.ok)
        self.assertTrue(all(st == {"started"} for st in self.states(fake).values()))

    def test_absent_apps_are_reported_not_fatal(self):
        fly, fake = setup(self.N, components=["mongodb", "pdp"])
        for c in ("mongodb", "pdp"):
            fly.run("deploy", "-a", self.N.app(c))
        report = stop_instance(fly, self.N)
        self.assertTrue(report.ok)
        self.assertEqual(sorted(report.changed), sorted([self.N.app("mongodb"), self.N.app("pdp")]))
        self.assertIn(self.N.app("vc-apigw"), report.absent)

    def test_scale_to_zero_stops_machines_waking_themselves(self):
        from sirosid_core.assets import write_fly_toml
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        on, off = d / "on.toml", d / "off.toml"
        write_fly_toml(on, "a", 8080, region="arn")
        write_fly_toml(off, "a", 8080, region="arn", autostart=False)
        self.assertIn("auto_start_machines = true", on.read_text())
        self.assertIn("auto_start_machines = false", off.read_text())
        self.assertIn("auto_stop_machines = 'off'", off.read_text(), "the control plane stops it, not Fly's proxy")


if __name__ == "__main__":
    unittest.main()
