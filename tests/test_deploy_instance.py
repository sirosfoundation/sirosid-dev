"""deploy_instance, called as a library: no CLI, no argv, no repo-relative guessing.

This is what a hosted service does, so these are the properties it relies on:
the deploy acts in the client's org as the client's identity, names everything
from the Naming it is given, reports failure as data, and a redeploy from saved
state is a real redeploy.
"""
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
from sirosid_core import state  # noqa: E402
from sirosid_core.deploy import DeployError, RegistrationError, deploy_instance  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_core.spec import InstanceSpec  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
NAMING = Naming("k7m2qx4d", app_prefix="sid", host_pattern="{env}-{component}.sandbox.example")


class FailingDeploy(FakeFly):
    def __init__(self, fail_app):
        super().__init__()
        self.fail_app = fail_app

    def handle(self, argv):
        if argv[:1] == ["deploy"] and self._opt(argv, "-a") == self.fail_app:
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 7, "", "build failed")
        return super().handle(argv)


def client(fake, org="sandbox", token="FlyV1 sandbox-token"):
    ok = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0)
    return FlyClient(org=org, token=token, runner=fake.runner(), docker=ok, out=lambda m: None, err=lambda m: None)


@NEEDS
class DeployInstanceTests(unittest.TestCase):
    def setUp(self):
        self.dirs = []
        self.addCleanup(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self.dirs])

    def root(self):
        d = Path(tempfile.mkdtemp(prefix="deploytest-"))
        self.dirs.append(d)
        return d

    def deploy(self, fake=None, spec=None, **kw):
        fake = fake or FakeFly()
        spec = spec or InstanceSpec(env=NAMING.env, region="arn", app_prefix="sid", host_pattern=NAMING.host_pattern)
        kw.setdefault("rendered_root", self.root())
        kw.setdefault("progress", lambda m: None)
        return fake, deploy_instance(spec, client(fake), NAMING, Resources(ROOT), **kw), kw["rendered_root"]

    def test_acts_in_the_clients_org_as_the_clients_identity(self):
        fake, result, _ = self.deploy()
        creates = [c for c in fake.log if " apps create " in c]
        self.assertTrue(creates)
        self.assertTrue(all("-o sandbox" in c and "sirosfoundation" not in c for c in creates))
        self.assertEqual(set(fake.tokens_seen), {"FlyV1 sandbox-token"},
                         "every flyctl call - including the deploys - carries the client's token")
        self.assertTrue(any(c.startswith("flyctl deploy") for c in fake.log))

    def test_everything_is_named_from_the_naming_it_was_given(self):
        fake, result, root = self.deploy()
        apps = [c.split()[3] for c in fake.log if " apps create " in c]
        self.assertTrue(all(a.startswith("sid-k7m2qx4d-") for a in apps), apps)
        self.assertEqual(result.urls["wallet-frontend"], "https://k7m2qx4d-wallet-frontend.sandbox.example")
        leaks = [c[:140] for c in fake.log if "sirosid-k7m2qx4d" in c or ".fly.dev" in c]
        self.assertEqual(leaks, [])

    def test_returns_what_a_caller_needs(self):
        _, result, _ = self.deploy()
        self.assertEqual(result.deployed[0], "mongodb")
        self.assertIn("wallet-frontend", result.deployed)
        self.assertEqual(len(result.admin_token), 32)
        self.assertEqual(result.images["pdp"], result.images["pdp"].strip())
        self.assertIn("mongodb", result.images)
        self.assertFalse(result.rendered_only)

    def test_render_only_deploys_nothing(self):
        fake, result, _ = self.deploy(render_only=True)
        self.assertTrue(result.rendered_only)
        self.assertFalse([c for c in fake.log if " apps create " in c or c.startswith("flyctl deploy")])
        self.assertIn("vc-apigw", result.images)

    def test_a_failed_component_is_reported_with_where_it_stopped(self):
        fake = FailingDeploy("sid-k7m2qx4d-pdp")
        with self.assertRaises(DeployError) as cm:
            self.deploy(fake=fake)
        e = cm.exception
        self.assertEqual(e.component, "pdp")
        self.assertEqual(e.returncode, 7)
        self.assertIn("mongodb", e.deployed)
        self.assertNotIn("pdp", e.deployed)
        self.assertIn("sid-k7m2qx4d-mongodb", fake.apps, "components that deployed are left running, not rolled back")

    def test_the_library_writes_nothing_to_stdout(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.deploy(progress=None)
        self.assertEqual(out.getvalue(), "", "a service must not have a library writing to its stdout")

    def test_a_region_is_required(self):
        with self.assertRaises(DeployError):
            self.deploy(spec=InstanceSpec(env=NAMING.env))

    def test_a_bad_android_entry_is_a_deploy_error(self):
        spec = InstanceSpec(env=NAMING.env, region="arn", android_apps=["no-equals"])
        with self.assertRaises(DeployError):
            self.deploy(spec=spec)

    def test_registration_runs_once_with_the_instances_urls(self):
        calls = []

        def register(admin_url, token, issuer_url, verifier_url):
            calls.append((admin_url, issuer_url, verifier_url, bool(token)))
            return {"issuer": "registered", "verifier": "registered"}
        self.deploy(register=register)
        self.assertEqual(calls, [("https://k7m2qx4d-wallet-proxy.sandbox.example",
                                  "https://k7m2qx4d-vc-apigw.sandbox.example",
                                  "https://k7m2qx4d-vc-verifier.sandbox.example", True)])

    def test_registration_that_never_succeeds_is_fatal_not_silent(self):
        import sirosid_core.deploy as d

        def never(*a):
            raise RegistrationError("not ready")
        real_sleep = d.time.sleep
        d.time.sleep = lambda s: None
        self.addCleanup(lambda: setattr(d.time, "sleep", real_sleep))
        with self.assertRaises(DeployError) as cm:
            self.deploy(register=never)
        self.assertIn("could not register", str(cm.exception))

    def test_a_service_style_redeploy_from_saved_state(self):
        """Seed a fresh scratch directory from a StateStore each time - what a
        service with no persistent disk does - and redeploy against the same Fly."""
        fake, store = FakeFly(), state.MemoryStateStore()
        spec = InstanceSpec(env=NAMING.env, region="arn", app_prefix="sid", host_pattern=NAMING.host_pattern)
        with state.workdir(store, "i1", root=self.root(), subdir="fly-k7m2qx4d") as scratch:
            first = deploy_instance(spec, client(fake), NAMING, Resources(ROOT), rendered_root=scratch,
                                    progress=lambda m: None)
        saved = store.load("i1")
        self.assertIn("mongoRootPassword", saved)
        n = len(fake.log)
        with state.workdir(store, "i1", root=self.root(), subdir="fly-k7m2qx4d") as scratch:
            second = deploy_instance(spec, client(fake), NAMING, Resources(ROOT), rendered_root=scratch,
                                     progress=lambda m: None)
        again = fake.log[n:]
        self.assertFalse([c for c in again if " apps create " in c or "volumes create" in c])
        self.assertEqual(second.admin_token, first.admin_token)
        self.assertEqual(second.mongo_password, first.mongo_password)


@NEEDS
class CliFailurePath(unittest.TestCase):
    """The same failure through scripts/fly-up.py: a script must exit non-zero, not traceback."""

    def test_cli_exits_one_and_stops_at_the_failed_component(self):
        import fakefly
        fake = FailingDeploy("sirosid-coretest-pdp")
        r = fakefly.run_fly_up([], fake=fake)
        self.assertEqual(r.exit, 1)
        deployed = [c.split()[3] for c in r.commands if c.startswith("flyctl deploy")]
        self.assertIn("sirosid-coretest-mongodb", deployed)
        self.assertEqual(deployed[-1], "sirosid-coretest-pdp", "nothing is attempted after the failure")
        self.assertIn("sirosid-coretest-mongodb", fake.apps, "components that deployed are left running")


if __name__ == "__main__":
    unittest.main()
