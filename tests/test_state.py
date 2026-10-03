import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import state  # noqa: E402


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_declares_what_is_state(self):
        for rel in ("mongoRootPassword", "jwtSecret", "adminToken", "apiAuthKey.pem",
                    "vc-pki/rootCA.key", "vc-pki/sub/x.pem"):
            self.assertTrue(state.is_state(rel), rel)
        for rel in ("vc-apigw.yaml", "wallet-frontend.fly.toml", "assetlinks.json", "vc-pkix", "x/vc-pki/a"):
            self.assertFalse(state.is_state(rel), rel)

    def test_export_takes_only_state_and_round_trips(self):
        (self.tmp / "vc-pki").mkdir()
        (self.tmp / "vc-pki" / "rootCA.key").write_bytes(b"KEY")
        (self.tmp / "adminToken").write_text("tok")
        (self.tmp / "vc-apigw.yaml").write_text("output")
        blobs = state.export_state(self.tmp)
        self.assertEqual(sorted(blobs), ["adminToken", "vc-pki/rootCA.key"])
        other = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(other, ignore_errors=True))
        state.import_state(other, blobs)
        self.assertEqual((other / "vc-pki" / "rootCA.key").read_bytes(), b"KEY")
        self.assertEqual(state.export_state(other), blobs)

    def test_import_refuses_non_state_and_traversal(self):
        with self.assertRaises(ValueError):
            state.import_state(self.tmp, {"vc-apigw.yaml": b"x"})
        with self.assertRaises(ValueError):
            state.import_state(self.tmp, {"vc-pki/../../etc/evil": b"x"})
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_restored_files_are_private(self):
        state.import_state(self.tmp, {"jwtSecret": b"s"})
        self.assertEqual((self.tmp / "jwtSecret").stat().st_mode & 0o777, 0o600)

    def test_persistent_secret_is_generated_once(self):
        a = state.persistent_secret(self.tmp, "adminToken")
        self.assertEqual(a, state.persistent_secret(self.tmp, "adminToken"))
        self.assertEqual(len(a), 32)
        with self.assertRaises(ValueError):
            state.persistent_secret(self.tmp, "not-declared")

    def test_workdir_saves_on_success_and_cleans_up(self):
        store = state.MemoryStateStore()
        with state.workdir(store, "i1") as d:
            state.persistent_secret(d, "adminToken")
            (d / "output.yaml").write_text("regenerable")
            seen = d
        self.assertFalse(seen.exists())
        self.assertEqual(sorted(store.load("i1")), ["adminToken"])

    def test_workdir_does_not_save_when_the_deploy_fails(self):
        store = state.MemoryStateStore()
        store.save("i1", {"adminToken": b"known-good"})
        with self.assertRaises(RuntimeError):
            with state.workdir(store, "i1") as d:
                (d / "adminToken").write_text("half-finished")
                raise RuntimeError("deploy failed")
        self.assertEqual(store.load("i1")["adminToken"], b"known-good")

    def test_workdir_seeds_from_saved_state(self):
        store = state.MemoryStateStore()
        store.save("i1", {"adminToken": b"abc"})
        with state.workdir(store, "i1") as d:
            self.assertEqual(state.persistent_secret(d, "adminToken"), "abc")


# --- the contract, end to end ------------------------------------------------
import shutil  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakefly  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")


@NEEDS
class RedeployFromExportedState(unittest.TestCase):
    """A service has no persistent disk: it keeps state.export_state() and seeds a
    scratch directory with it. That must be enough for a redeploy to be a real
    redeploy - same secrets, nothing recreated, nothing refused."""

    def setUp(self):
        self.dirs = []
        self.addCleanup(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self.dirs])

    def scratch(self):
        d = Path(tempfile.mkdtemp(prefix="coretest-root-"))
        self.dirs.append(d)
        return d

    def deploy(self, fake, state=None):
        root = self.scratch()
        r = fakefly.run_fly_up([], fake=fake, rendered_root=root, state=state)
        return r, root / "fly-coretest"

    def test_state_alone_is_enough_for_a_redeploy(self):
        fake = fakefly.FakeFly()
        first, dir1 = self.deploy(fake)
        self.assertEqual(first.exit, 0)
        blobs = state.export_state(dir1)
        self.assertIn("mongoRootPassword", blobs)
        self.assertTrue(any(k.startswith("vc-pki/") for k in blobs))
        n = len(fake.log)

        second, dir2 = self.deploy(fake, state=blobs)       # fresh directory, state only
        self.assertEqual(second.exit, 0, getattr(second, "error", ""))
        issued = second.commands[n:]
        self.assertEqual([c for c in issued if " apps create " in c or "volumes create" in c], [])
        # The only secrets a redeploy sets are env-admin's, by design: it gets freshly
        # minted app-scoped deploy tokens and the (unchanged) mongo URI on every run.
        # Every generated-once secret is left alone.
        reset = sorted(c.split()[3].split("=")[0] + " @ " + c.split()[5] for c in issued if "secrets set" in c)
        self.assertEqual(reset, ["flyApiTokens @ sirosid-coretest-env-admin", "mongoUri @ sirosid-coretest-env-admin"])
        # The secrets the second run used are the ones the first generated.
        for name in ("mongoRootPassword", "jwtSecret", "adminToken", "apiAuthKey.pem"):
            self.assertEqual((dir2 / name).read_bytes(), (dir1 / name).read_bytes(), name)
        self.assertEqual(state.export_state(dir2), blobs, "a redeploy must not change state")
        # And every rendered config that embeds one of them is identical.
        for rel in ("wallet-backend.yaml", "vc-apigw.yaml", "vc-issuer.yaml", "vc-registry.yaml", "vc-verifier.yaml"):
            self.assertEqual((dir2 / rel).read_bytes(), (dir1 / rel).read_bytes(), rel)

    def test_without_state_the_redeploy_is_refused_not_silently_wrong(self):
        fake = fakefly.FakeFly()
        first, _ = self.deploy(fake)
        self.assertEqual(first.exit, 0)
        second, _ = self.deploy(fake, state=None)           # state lost
        self.assertNotEqual(second.exit, 0, "deploying a guessed mongo password would lock consumers out of their data")
        self.assertIn("no cached copy", getattr(second, "error", ""), "and it must say why")

    def test_exported_state_contains_only_declared_files(self):
        _, dir1 = self.deploy(fakefly.FakeFly())
        blobs = state.export_state(dir1)
        self.assertTrue(all(state.is_state(k) for k in blobs))
        self.assertNotIn("vc-apigw.yaml", blobs)


if __name__ == "__main__":
    unittest.main()
