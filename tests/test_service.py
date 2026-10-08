"""The control plane against a fake Fly: who may do what, quotas, the reaper, the sweeper."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402
from sirosid_core.policy import CAP_CUSTOM_IMAGES, PlatformPolicy, PolicyError  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_service.db import Database  # noqa: E402
from sirosid_service.service import (DAY, ControlPlane, Forbidden, InvalidInvite, InvalidState, Limits, NotFound,  # noqa: E402
                                     Principal, QuotaExceeded, ServiceError)

NEEDS = unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
KEY = bytes(range(32))
IDS = ["aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee", "ffffffff", "gggggggg", "hhhhhhhh", "iiiiiiii", "jjjjjjjj",
       "kkkkkkkk", "llllllll"]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def advance(self, days=0, seconds=0):
        self.t += days * DAY + seconds


class FailingDeploy(FakeFly):
    def handle(self, argv):
        if argv[:1] == ["deploy"] and "pdp" in (self._opt(argv, "-a") or ""):
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 5, "", "boom")
        return super().handle(argv)


_OPEN = []


def tearDownModule():
    for db in _OPEN:
        db.close()


def make(fake=None, limits=None, platform=None):
    clock = Clock()
    fake = fake or FakeFly()
    fly = FlyClient(org="sandbox", token="tok", runner=fake.runner(), out=lambda m: None, err=lambda m: None)
    ids = iter(IDS)
    db = Database(clock=clock)
    _OPEN.append(db)
    cp = ControlPlane(db, fly, Resources(ROOT), platform=platform or PlatformPolicy(env_admin=False),
                      limits=limits, clock=clock, id_generator=lambda: next(ids))
    return cp, fake, clock


def admin_and_user(cp, caps=(), **invite):
    admin = cp.bootstrap_admin("Root", "root@example.com")
    token = cp.create_invite(admin, capabilities=caps, **invite)
    alice = cp.redeem_invite(token, "Alice", "alice@example.com")
    return admin, cp.begin_session(alice.user_id, KEY)      # the user has unlocked with their passkey


class InviteTests(unittest.TestCase):
    def test_first_admin_only_once(self):
        cp, _, _ = make()
        cp.bootstrap_admin("Root")
        with self.assertRaises(Forbidden):
            cp.bootstrap_admin("Other")

    def test_only_admins_invite(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        with self.assertRaises(Forbidden):
            cp.create_invite(alice)
        with self.assertRaises(Forbidden):
            cp.list_invites(alice)

    def test_an_invite_is_single_use(self):
        cp, _, _ = make()
        admin = cp.bootstrap_admin("Root")
        tok = cp.create_invite(admin)
        cp.redeem_invite(tok, "A")
        with self.assertRaises(InvalidInvite):
            cp.redeem_invite(tok, "B")

    def test_the_token_is_stored_only_as_a_hash(self):
        cp, _, _ = make()
        admin = cp.bootstrap_admin("Root")
        tok = cp.create_invite(admin)
        dump = json.dumps(cp.db.all("SELECT * FROM invites")) + json.dumps(cp.db.audit_log())
        self.assertNotIn(tok, dump)

    def test_expired_revoked_unknown_and_wrongly_bound_invites_fail_alike(self):
        cp, _, clock = make()
        admin = cp.bootstrap_admin("Root")
        expired = cp.create_invite(admin, days_valid=1)
        clock.advance(days=2)
        revoked = cp.create_invite(admin)
        cp.revoke_invite(admin, cp.db.all("SELECT token_hash FROM invites WHERE revoked=0 ORDER BY created_at DESC")[0]["token_hash"][:12])
        bound = cp.create_invite(admin, email="a@example.com")
        for tok, email in ((expired, ""), (revoked, ""), ("nonsense", ""), (bound, "b@example.com")):
            with self.assertRaises(InvalidInvite):
                cp.redeem_invite(tok, "X", email)
        self.assertEqual(cp.redeem_invite(bound, "A", " A@Example.com ").role, "member")

    def test_bad_invite_parameters(self):
        cp, _, _ = make()
        admin = cp.bootstrap_admin("Root")
        for kw in ({"capabilities": ["root"]}, {"role": "god"}, {"days_valid": 0}, {"days_valid": 9999}):
            with self.assertRaises(ServiceError):
                cp.create_invite(admin, **kw)

    def test_capabilities_travel_with_the_invite(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp, caps=[CAP_CUSTOM_IMAGES])
        self.assertIn(CAP_CUSTOM_IMAGES, alice.capabilities)

    def test_a_disabled_user_cannot_get_a_principal(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        cp.disable_user(admin, alice.user_id)
        with self.assertRaises(Forbidden):
            cp.principal_for(alice.user_id)

    def test_grant_changes_capabilities_and_limits(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        cp.grant(admin, alice.user_id, capabilities=[CAP_CUSTOM_IMAGES], max_concurrent=5, max_kept=2, kept_for_days=30)
        a = cp.principal_for(alice.user_id)
        self.assertIn(CAP_CUSTOM_IMAGES, a.capabilities)
        with self.assertRaises(Forbidden):
            cp.grant(a, alice.user_id, max_concurrent=99)


class ConfigTests(unittest.TestCase):
    def test_save_validates_with_the_callers_capabilities(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        with self.assertRaises(PolicyError):
            cp.save_config(alice, "x", {"images": {"pdp": "ghcr.io/me/pdp:1"}})
        self.assertEqual(cp.save_config(alice, "ok", {"trusted_issuers": ["https://i.example.com"]})["name"], "ok")

    def test_configs_are_private_to_their_owner(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        bob = cp.redeem_invite(cp.create_invite(admin), "Bob")
        cp.save_config(alice, "mine", {"name": "mine"})
        self.assertEqual([c["name"] for c in cp.list_configs(alice)], ["mine"])
        self.assertEqual(cp.list_configs(bob), [])
        with self.assertRaises(NotFound):
            cp.get_config(bob, "mine")

    def test_saving_again_updates_in_place(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        a = cp.save_config(alice, "c", {"wallet_attestation": False})
        b = cp.save_config(alice, "c", {"wallet_attestation": True})
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(cp.get_config(alice, "c")["wallet_attestation"])

    def test_validate_reports_problems_as_text(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        got = cp.validate_config(alice, {"trusted_issuers": ["http://x"], "wat": 1})
        self.assertEqual(len(got), 2)

    def test_schema_is_exposed(self):
        cp, _, _ = make()
        self.assertIn("images", cp.schema()["properties"])


@NEEDS
class InstanceTests(unittest.TestCase):
    def test_create_runs_the_deploy_and_names_things_from_the_platform(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        inst = cp.create_instance(alice, name="demo")
        self.assertEqual((inst["id"], inst["status"]), ("aaaaaaaa", "running"))
        apps = sorted(fake.apps)
        self.assertTrue(apps and all(a.startswith("sid-aaaaaaaa-") for a in apps), apps)
        self.assertTrue(all("-o sandbox" in c for c in fake.log if " apps create " in c))
        self.assertEqual(set(fake.tokens_seen), {"tok"})
        self.assertIn("wallet-frontend", inst["urls"])

    def test_an_unexpected_error_in_the_deploy_job_ends_in_failed_not_creating_forever(self):
        """Seen on real Fly: a PermissionError (not a DeployError) escaped the job, the
        thread pool swallowed it and the instance said 'creating' for good."""
        import unittest.mock as mock
        import sirosid_service.service as svc
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        with mock.patch.object(svc, "deploy_instance", side_effect=PermissionError(13, "Permission denied", "/app/x")), \
                self.assertLogs("sirosid.service", "ERROR"):
            inst = cp.create_instance(alice, name="demo")
        self.assertEqual(inst["status"], "failed")
        self.assertIn("internal error during deploy (PermissionError", inst["error"])
        self.assertIn("deploy_failed", [r["action"] for r in cp.db.audit_log()])

    def test_the_thread_runner_logs_what_a_job_raises(self):
        import sirosid_service.service as svc
        r = svc.ThreadRunner(workers=1)
        with self.assertLogs("sirosid.service", "ERROR") as logs:
            r.submit(lambda: 1 / 0)
            r._pool.shutdown(wait=True)
        self.assertIn("ZeroDivisionError", "\n".join(logs.output))

    def test_ttl_is_three_days(self):
        cp, _, clock = make()
        _, alice = admin_and_user(cp)
        inst = cp.create_instance(alice)
        self.assertAlmostEqual(inst["expires_at"] - clock(), 3 * DAY, delta=1)

    def test_a_user_sees_only_their_own_instances_and_ids_cannot_be_probed(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        bob = cp.redeem_invite(cp.create_invite(admin), "Bob")
        a = cp.create_instance(alice)
        self.assertEqual(cp.list_instances(bob), [])
        for call in (cp.get_instance, cp.stop_instance, cp.destroy_instance, cp.instance_credentials):
            with self.assertRaises(NotFound) as e1:
                call(bob, a["id"])
            with self.assertRaises(NotFound) as e2:
                call(bob, "zzzzzzzz")
            self.assertEqual(str(e1.exception), str(e2.exception))

    def test_per_user_and_global_quotas(self):
        cp, _, _ = make(limits=Limits(global_max_instances=3))
        admin, alice = admin_and_user(cp, max_concurrent=2)
        bob = cp.begin_session(cp.redeem_invite(cp.create_invite(admin, max_concurrent=5), "Bob").user_id, bytes(range(1, 33)))
        cp.create_instance(alice); cp.create_instance(alice)
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(alice)
        cp.create_instance(bob)
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(bob)                      # global cap of 3 reached

    def test_a_failed_deploy_is_recorded_and_still_counts(self):
        cp, _, _ = make(fake=FailingDeploy())
        _, alice = admin_and_user(cp, max_concurrent=1)
        inst = cp.create_instance(alice)
        self.assertEqual(inst["status"], "failed")
        self.assertIn("pdp", inst["error"])
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(alice)
        cp.destroy_instance(alice, inst["id"])
        cp.create_instance(alice)                      # the quota slot is free again (it fails too: this fake always does)

    def test_credentials_are_the_owners_not_an_admins(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        a = cp.create_instance(alice)
        creds = cp.instance_credentials(alice, a["id"])
        self.assertEqual(len(creds["admin_token"]), 32)
        with self.assertRaises(NotFound):
            cp.instance_credentials(admin, a["id"])
        self.assertEqual(cp.get_instance(admin, a["id"])["id"], a["id"])        # but may see it
        self.assertEqual(len(cp.list_instances(admin, all_users=True)), 1)
        with self.assertRaises(Forbidden):
            cp.list_instances(alice, all_users=True)

    def test_stop_start_and_invalid_transitions(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        a = cp.create_instance(alice)["id"]
        self.assertEqual(cp.stop_instance(alice, a)["status"], "stopped")
        self.assertTrue(all(m["state"] == "stopped" for v in fake.apps.values() for m in v["machines"]))
        with self.assertRaises(InvalidState):
            cp.stop_instance(alice, a)
        self.assertEqual(cp.start_instance(alice, a)["status"], "running")
        with self.assertRaises(InvalidState):
            cp.start_instance(alice, a)

    def test_destroy_removes_everything(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        a = cp.create_instance(alice)["id"]
        self.assertEqual(cp.destroy_instance(alice, a)["status"], "destroyed")
        self.assertEqual(fake.apps, {})
        self.assertEqual(cp.db.all("SELECT * FROM state WHERE instance_id=?", (a,)), [])
        self.assertEqual(cp.list_instances(alice), [])
        self.assertEqual(cp.create_instance(alice)["status"], "running")        # quota freed

    def test_reset_wipes_data_but_keeps_secrets(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        a = cp.create_instance(alice)["id"]
        mongo = f"sid-{a}-mongodb"
        old_vol = fake.apps[mongo]["volumes"][0]["id"]
        token = cp.instance_credentials(alice, a)["admin_token"]
        out = cp.reset_instance(alice, a)
        self.assertEqual(out["status"], "running")
        vols = [v for v in fake.apps[mongo]["volumes"] if v.get("state") != "destroyed"]
        self.assertEqual(len(vols), 1)
        self.assertNotEqual(vols[0]["id"], old_vol, "a fresh volume")
        self.assertEqual(cp.instance_credentials(alice, a)["admin_token"], token)
        self.assertTrue(any("volumes destroy" in c for c in fake.log))

    def test_audit_log_records_who_did_what(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        a = cp.create_instance(alice)["id"]
        cp.destroy_instance(alice, a)
        actions = [(r["actor"], r["action"]) for r in cp.db.audit_log()]
        self.assertIn((alice.user_id, "create_instance"), actions)
        self.assertIn((alice.user_id, "destroy_instance"), actions)


@NEEDS
class CustomImageTests(unittest.TestCase):
    def test_images_need_the_capability_at_creation_time_not_save_time(self):
        cp, fake, _ = make()
        admin, alice = admin_and_user(cp, caps=[CAP_CUSTOM_IMAGES])
        cfg = {"images": {"pdp": "ghcr.io/me/pdp:9.9"}}
        cp.save_config(alice, "dev", cfg)
        cp.grant(admin, alice.user_id, capabilities=[])            # withdrawn after saving
        with self.assertRaises(PolicyError):
            cp.create_instance(cp.principal_for(alice.user_id, alice.session_id), config_name="dev")
        cp.grant(admin, alice.user_id, capabilities=[CAP_CUSTOM_IMAGES])
        cp.create_instance(cp.principal_for(alice.user_id, alice.session_id), config_name="dev")
        deploys = [c for c in fake.log if c.startswith("flyctl deploy") and "-pdp " in c]
        self.assertTrue(any("-i ghcr.io/me/pdp:9.9" in c for c in deploys), deploys)

    def test_a_bare_local_image_never_reaches_the_deploy(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp, caps=[CAP_CUSTOM_IMAGES])
        with self.assertRaises(PolicyError):
            cp.create_instance(alice, config={"images": {"pdp": "my-local-image:dev"}})
        self.assertFalse([c for c in fake.log if c.startswith("docker")])


@NEEDS
class KeepAndExpiryTests(unittest.TestCase):
    def test_keeping_needs_an_allowance(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(alice, keep=True)

    def test_a_kept_instance_does_not_expire_and_the_allowance_is_counted(self):
        cp, _, clock = make()
        _, alice = admin_and_user(cp, max_kept=1, kept_for_days=30)
        k = cp.create_instance(alice, keep=True)
        self.assertIsNone(k["expires_at"])
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(alice, keep=True)
        clock.advance(days=10)
        self.assertEqual(cp.reap(), [])

    def test_unkeeping_puts_it_back_on_the_clock(self):
        cp, _, clock = make()
        _, alice = admin_and_user(cp, max_kept=1, kept_for_days=30)
        k = cp.create_instance(alice, keep=True)["id"]
        out = cp.set_keep(alice, k, False)
        self.assertAlmostEqual(out["expires_at"] - clock(), 3 * DAY, delta=1)

    def test_the_reaper_destroys_only_what_is_due(self):
        cp, fake, clock = make()
        admin, alice = admin_and_user(cp, max_concurrent=3, max_kept=1, kept_for_days=30)
        old = cp.create_instance(alice)["id"]
        keeper = cp.create_instance(alice, keep=True)["id"]
        clock.advance(days=2)
        alice = cp.begin_session(alice.user_id, KEY)      # the 8-hour session expired; unlock again
        young = cp.create_instance(alice)["id"]
        clock.advance(days=1.5)                       # old is 3.5 days, young 1.5, keeper has no clock
        self.assertEqual(cp.reap(), [old])
        self.assertEqual(sorted(i["id"] for i in cp.list_instances(alice)), sorted([young, keeper]))
        self.assertFalse([a for a in fake.apps if a.startswith(f"sid-{old}-")])

    def test_a_lapsed_allowance_gives_a_days_grace_not_instant_deletion(self):
        cp, _, clock = make()
        _, alice = admin_and_user(cp, max_kept=1, kept_for_days=5)
        k = cp.create_instance(alice, keep=True)["id"]
        clock.advance(days=6)
        self.assertEqual(cp.lapse_kept(), [k])
        self.assertEqual(cp.reap(), [])
        clock.advance(days=1.1)
        self.assertEqual(cp.reap(), [k])

    def test_disabling_a_user_starts_the_clock_on_everything_they_have(self):
        cp, _, clock = make()
        admin, alice = admin_and_user(cp, max_kept=1, kept_for_days=30)
        k = cp.create_instance(alice, keep=True)["id"]
        cp.disable_user(admin, alice.user_id)
        self.assertEqual(cp.reap(), [])
        clock.advance(days=1.1)
        self.assertEqual(cp.reap(), [k])


@NEEDS
class SweeperTests(unittest.TestCase):
    def fly_of(self, fake):
        return FlyClient(org="sandbox", token="tok", runner=fake.runner(), out=lambda m: None, err=lambda m: None)

    def test_an_unknown_instance_is_destroyed_only_after_the_grace_period(self):
        cp, fake, clock = make()
        fly = self.fly_of(fake)
        stray = Naming("zzzzzzzz", app_prefix="sid")
        for c in ("mongodb", "pdp"):
            fly.ensure_app(stray.app(c))
        out = cp.sweep_orphans()
        self.assertEqual((out["orphans"], out["destroyed"]), (["zzzzzzzz"], []))
        clock.advance(seconds=1800)
        self.assertEqual(cp.sweep_orphans()["destroyed"], [])
        clock.advance(seconds=1900)
        self.assertEqual(cp.sweep_orphans()["destroyed"], ["zzzzzzzz"])
        self.assertEqual(fake.apps, {})

    def test_live_instances_and_foreign_apps_are_never_touched(self):
        cp, fake, clock = make()
        _, alice = admin_and_user(cp)
        live = cp.create_instance(alice)["id"]
        fly = self.fly_of(fake)
        foreign = ["sirosid-gdc-pdp", "sid-short-pdp", "sid-zzzzzzzz-notacomponent", "other-zzzzzzzz-pdp", "sid-ZZZZZZZZ-pdp"]
        for a in foreign:
            fly.ensure_app(a)
        clock.advance(days=1)
        out = cp.sweep_orphans()
        self.assertEqual(out["orphans"], [])
        self.assertEqual(out["destroyed"], [])
        for a in foreign:
            self.assertIn(a, fake.apps)
        self.assertTrue(any(a.startswith(f"sid-{live}-") for a in fake.apps))

    def test_an_app_that_stops_being_an_orphan_resets_its_grace(self):
        cp, fake, clock = make()
        fly = self.fly_of(fake)
        stray = Naming("zzzzzzzz", app_prefix="sid")
        fly.ensure_app(stray.app("pdp"))
        cp.sweep_orphans()
        fake.apps.pop(stray.app("pdp"))
        cp.sweep_orphans()
        self.assertEqual(cp.db.all("SELECT * FROM orphans"), [])

    def test_tick_runs_everything(self):
        cp, _, _ = make()
        self.assertEqual(sorted(cp.tick()), ["lapsed", "reaped", "sweep"])


SINGLE = dict(env_admin=False, layout="single-machine", host_pattern="{component}-{id}.sid.example")


def make_single(**platform):
    """A control plane whose platform deploys single-machine instances, against
    fake flyctl + a fake Machines API + a fake registry."""
    from fakemachines import FakeMachines
    from sirosid_core.machines import MachinesClient
    from sirosid_core.oci import Registry
    fake = FakeFly()
    api = FakeMachines(fake)
    clock = Clock()
    fly = FlyClient(org="sandbox", token="tok", runner=fake.runner(),
                    docker=lambda cmd, **k: subprocess.CompletedProcess(cmd, 0), out=lambda m: None, err=lambda m: None)
    ids = iter(IDS)
    db = Database(clock=clock)
    _OPEN.append(db)
    registered = []
    cp = ControlPlane(db, fly, Resources(ROOT), platform=PlatformPolicy(**{**SINGLE, **platform}), clock=clock,
                      id_generator=lambda: next(ids), machines=MachinesClient("tok", transport=api.transport,
                                                                                sleep=lambda s: None),
                      register=lambda *a: registered.append(a) or {"issuer": "registered", "verifier": "registered"},
                      registry=Registry("registry.fly.io", "x", "tok", transport=api.registry_transport))
    return cp, fake, api, clock, registered, lambda: None


@NEEDS
class SingleMachineServiceTests(unittest.TestCase):
    def setUp(self):
        self.cp, self.fake, self.api, self.clock, self.registered, undo = make_single()
        self.addCleanup(undo)
        _, self.alice = admin_and_user(self.cp)

    def test_create_deploys_one_app_and_stores_the_layout_in_plaintext(self):
        inst = self.cp.create_instance(self.alice)
        self.assertEqual(inst["status"], "running", inst["error"])
        self.assertEqual(sorted(self.fake.apps), ["sid-aaaaaaaa"], "one app named sid-<id> (what the edge targets)")
        self.assertEqual(inst["urls"]["vc-apigw"], "https://vc-apigw-aaaaaaaa.sid.example")
        row = self.cp.db.one("SELECT naming FROM instances WHERE id=?", ("aaaaaaaa",))
        self.assertEqual(json.loads(row["naming"])["layout"], "single-machine")
        self.assertTrue(self.registered)

    def test_stop_start_destroy_need_no_session(self):
        a = self.cp.create_instance(self.alice)["id"]
        locked = self.cp.principal_for(self.alice.user_id)          # no session: data is sealed
        self.assertEqual(self.cp.stop_instance(locked, a)["status"], "stopped")
        self.assertEqual(self.api.only("sid-aaaaaaaa")["state"], "stopped")
        self.assertEqual(self.cp.start_instance(locked, a)["status"], "running")
        self.assertEqual(self.cp.destroy_instance(locked, a)["status"], "destroyed")
        self.assertEqual(self.fake.apps, {})

    def test_the_reaper_destroys_a_single_machine_instance(self):
        a = self.cp.create_instance(self.alice)["id"]
        self.clock.advance(days=4)
        self.assertEqual(self.cp.reap(), [a])
        self.assertEqual(self.fake.apps, {})

    def test_reset_drops_data_in_place_and_registers_again(self):
        a = self.cp.create_instance(self.alice)["id"]
        n = len(self.registered)
        token = self.cp.instance_credentials(self.alice, a)["admin_token"]
        self.assertEqual(self.cp.reset_instance(self.alice, a)["status"], "running")
        m = self.api.only("sid-aaaaaaaa")
        self.assertEqual(m["execs"][-1]["container"], "mongodb")
        self.assertEqual(len(self.registered), n + 1)
        self.assertEqual(self.registered[-1][1], token)
        self.assertFalse(any("volumes destroy" in c for c in self.fake.log), "in place: the volume stays")

    def test_the_sweeper_understands_the_single_app_name(self):
        live = self.cp.create_instance(self.alice)["id"]
        fly = FlyClient(org="sandbox", token="tok", runner=self.fake.runner(), out=lambda m: None, err=lambda m: None)
        fly.ensure_app("sid-zzzzzzzz")                 # an orphaned single-machine instance
        fly.ensure_app("sid-zzzzzzzz-pdp")             # and an apps-layout one with the same id
        for foreign in ("sid-zzzzzzz", "sid-zzzzzzzzz", "sid-ZZZZZZZZ", "sbx-zzzzzzzz"):
            fly.ensure_app(foreign)
        out = self.cp.sweep_orphans()
        self.assertEqual(out["orphans"], ["zzzzzzzz"])
        self.clock.advance(seconds=4000)
        self.assertEqual(self.cp.sweep_orphans()["destroyed"], ["zzzzzzzz"])
        self.assertNotIn("sid-zzzzzzzz", self.fake.apps)
        self.assertNotIn("sid-zzzzzzzz-pdp", self.fake.apps)
        for foreign in ("sid-zzzzzzz", "sid-zzzzzzzzz", "sid-ZZZZZZZZ", "sbx-zzzzzzzz", f"sid-{live}"):
            self.assertIn(foreign, self.fake.apps)

    def test_edge_mode_allocates_no_public_ips(self):
        cp, fake, api, _, _, undo = make_single(public_ips=False)
        self.addCleanup(undo)
        _, bob = admin_and_user(cp)
        inst = cp.create_instance(bob)
        self.assertEqual(inst["status"], "running", inst["error"])
        self.assertFalse([c for c in fake.log if " ips allocate" in c])
        self.assertFalse([c for c in fake.log if "--network" in c], "the org's default network, for fly-replay")
        self.assertNotIn("fly.dev", inst["urls"])

    def test_the_policy_refuses_conformance_in_this_layout(self):
        with self.assertRaises(PolicyError):
            self.cp.create_instance(self.alice, config={"conformance": True})


class SwitchableFailure(FakeFly):
    """A fake whose `flyctl deploy` of one component can be made to fail later."""
    fail_on = None

    def handle(self, argv):
        if self.fail_on and argv[:1] == ["deploy"] and self.fail_on in (self._opt(argv, "-a") or ""):
            self.log.append("flyctl " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 5, "", "boom")
        return super().handle(argv)


class DeferredRunner:
    """Holds background jobs until the test runs them: to interleave a job with other calls."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)


ISSUER = {"trusted_issuers": ["https://issuer.example.com"]}


def sealed_spec(cp, who, iid):
    return cp._spec(cp.db.one("SELECT * FROM instances WHERE id=?", (iid,)), cp._sealer(who))


@NEEDS
class JobResultTests(unittest.TestCase):
    def test_a_finished_deploy_cannot_resurrect_an_instance_destroyed_meanwhile(self):
        """create's job runs while the owner (or the reaper) destroys the instance: the job's result must be
        discarded, not written over `destroyed` as `running` with URLs for apps that are gone."""
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        cp.runner = DeferredRunner()
        iid = cp.create_instance(alice, name="demo")["id"]
        real = cp._deploy

        def deploy_then_get_destroyed(r, spec, sealer):
            out = real(r, spec, sealer)
            cp.destroy_instance(alice, iid)                  # destroyed while the job was still running
            return out
        cp._deploy = deploy_then_get_destroyed
        cp.runner.run_all()
        row = cp.db.one("SELECT status, urls FROM instances WHERE id=?", (iid,))
        self.assertEqual(row["status"], "destroyed")
        self.assertEqual(row["urls"], "{}")
        self.assertIn("job_result_discarded", [a["action"] for a in cp.db.audit_log(20)])


@NEEDS
class ReconfigureTests(unittest.TestCase):
    def deploys(self, fake, component):
        return [c for c in fake.log if c.startswith("flyctl deploy") and f"-{component} " in c + " "]

    def test_reconfigure_redeploys_with_the_new_config_and_keeps_data_and_secrets(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice, name="demo")["id"]
        mongo = f"sid-{iid}-mongodb"
        vol = fake.apps[mongo]["volumes"][0]["id"]
        token = cp.instance_credentials(alice, iid)["admin_token"]
        before = len(self.deploys(fake, "pdp"))
        out = cp.reconfigure_instance(alice, iid, config=ISSUER)
        self.assertEqual(out["status"], "running", out["error"])
        self.assertEqual(cp.get_instance_config(alice, iid), ISSUER)
        self.assertEqual(sealed_spec(cp, alice, iid).trusted_issuers, ISSUER["trusted_issuers"])
        self.assertGreater(len(self.deploys(fake, "pdp")), before, "redeployed")
        self.assertEqual([v["id"] for v in fake.apps[mongo]["volumes"] if v.get("state") != "destroyed"], [vol], "data kept")
        self.assertEqual(cp.instance_credentials(alice, iid)["admin_token"], token, "generated state kept")
        self.assertFalse(any("volumes destroy" in c for c in fake.log))
        row = cp.db.one("SELECT pending_spec, pending_config FROM instances WHERE id=?", (iid,))
        self.assertEqual((row["pending_spec"], row["pending_config"]), (None, None))
        acts = [r["action"] for r in cp.db.audit_log()]
        self.assertIn("reconfigure_instance", acts)
        self.assertIn("reconfigured", acts)

    def test_the_config_name_is_kept_as_plaintext_metadata_and_only_the_name(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        cp.save_config(alice, "partner", ISSUER)
        inst = cp.create_instance(alice, config_name="partner")
        self.assertEqual((inst["config_name"], inst["layout"], inst["reconfigurable"]), ("partner", "apps", True))
        self.assertIsNone(cp.create_instance(alice, config={}, config_name="partner")["config_name"], "inline wins")
        row = json.dumps({k: v for k, v in cp.db.one("SELECT * FROM instances WHERE id=?", (inst["id"],)).items()
                          if isinstance(v, str)})
        self.assertNotIn("issuer.example.com", row, "the config's content is never plaintext")
        cp.save_config(alice, "other", {})
        out = cp.reconfigure_instance(alice, inst["id"], config_name="other")
        self.assertEqual(out["config_name"], "other")
        self.assertIsNone(cp.reconfigure_instance(alice, inst["id"], config={})["config_name"])

    def test_every_policy_problem_at_once_and_nothing_changes(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice, config=ISSUER)["id"]
        n = len(fake.log)
        with self.assertRaises(PolicyError) as e:
            cp.reconfigure_instance(alice, iid, config={"trusted_issuers": ["http://x"], "mystery": 1})
        self.assertEqual(sorted(p.path for p in e.exception.problems), ["mystery", "trusted_issuers[0]"])
        self.assertEqual(cp.get_instance(alice, iid)["status"], "running")
        self.assertEqual(cp.get_instance_config(alice, iid), ISSUER)
        self.assertEqual(fake.log[n:], [], "nothing reached Fly")

    def test_capabilities_are_checked_now(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp, caps=[CAP_CUSTOM_IMAGES])
        iid = cp.create_instance(alice)["id"]
        cp.grant(admin, alice.user_id, capabilities=[])
        with self.assertRaises(PolicyError):
            cp.reconfigure_instance(cp.principal_for(alice.user_id, alice.session_id), iid,
                                    config={"images": {"pdp": "ghcr.io/me/pdp:1"}})

    def test_which_states_may_be_reconfigured(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        cp.stop_instance(alice, iid)
        self.assertFalse(cp.get_instance(alice, iid)["reconfigurable"])
        with self.assertRaises(InvalidState) as e:
            cp.reconfigure_instance(alice, iid, config={})
        self.assertIn("start it first", str(e.exception))
        self.assertEqual(cp.get_instance(alice, iid)["status"], "stopped", "left stopped, not started behind the user's back")
        for status in ("creating", "resetting", "reconfiguring", "destroyed"):
            cp._set(iid, status=status)
            with self.assertRaises(InvalidState, msg=status):
                cp.reconfigure_instance(alice, iid, config={})
        cp._set(iid, status="failed")
        self.assertEqual(cp.reconfigure_instance(alice, iid, config={})["status"], "running", "a failed one may be fixed")

    def test_locked_ownership_and_arguments(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        from sirosid_service.vault import Locked
        locked = cp.principal_for(alice.user_id)
        for call in (lambda: cp.reconfigure_instance(locked, iid, config={}), lambda: cp.get_instance_config(locked, iid)):
            with self.assertRaises(Locked):
                call()
        bob = cp.begin_session(cp.redeem_invite(cp.create_invite(admin), "Bob").user_id, bytes(range(1, 33)))
        for call in (cp.get_instance_config, cp.instance_health, cp.instance_activity,
                     lambda w, i: cp.reconfigure_instance(w, i, config={})):
            with self.assertRaises(NotFound) as e1:
                call(bob, iid)
            with self.assertRaises(NotFound) as e2:
                call(bob, "zzzzzzzz")
            self.assertEqual(str(e1.exception), str(e2.exception))
        with self.assertRaises(NotFound):
            cp.reconfigure_instance(cp.begin_session(admin.user_id, bytes(range(2, 34))), iid, config={})  # an admin may look, not reconfigure
        with self.assertRaises(ServiceError):
            cp.reconfigure_instance(alice, iid)
        with self.assertRaises(NotFound):
            cp.reconfigure_instance(alice, iid, config_name="no-such-config")

    def test_a_failed_redeploy_keeps_the_previous_config(self):
        """Mutation-checked: writing the new config before the redeploy (or not
        restoring it) fails this test."""
        fake = SwitchableFailure()
        cp, fake, _ = make(fake=fake)
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice, config=ISSUER)["id"]
        fake.fail_on = "pdp"
        out = cp.reconfigure_instance(alice, iid, config={"trusted_issuers": ["https://other.example.com"]})
        self.assertEqual(out["status"], "failed")
        self.assertIn("reconfigure failed", out["error"])
        self.assertIn("previous config", out["error"])
        self.assertEqual(cp.get_instance_config(alice, iid), ISSUER, "the working config is not lost")
        self.assertEqual(sealed_spec(cp, alice, iid).trusted_issuers, ISSUER["trusted_issuers"])
        self.assertIsNone(cp.db.one("SELECT pending_spec FROM instances WHERE id=?", (iid,))["pending_spec"])
        self.assertIn("reconfigure_failed", [r["action"] for r in cp.db.audit_log()])
        fake.fail_on = None
        self.assertEqual(cp.reconfigure_instance(alice, iid, config=cp.get_instance_config(alice, iid))["status"], "running",
                         "the way back: reconfigure with the previous config")

    def test_an_unexpected_error_in_the_job_also_ends_failed_and_keeps_the_config(self):
        import unittest.mock as mock
        import sirosid_service.service as svc
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice, config=ISSUER)["id"]
        with mock.patch.object(svc, "deploy_instance", side_effect=PermissionError(13, "denied")), \
                self.assertLogs("sirosid.service", "ERROR"):
            out = cp.reconfigure_instance(alice, iid, config={})
        self.assertEqual(out["status"], "failed")
        self.assertIn("internal error during reconfigure", out["error"])
        self.assertEqual(cp.get_instance_config(alice, iid), ISSUER)

    def test_the_instance_keeps_its_layout_and_naming_when_the_platform_changes(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        before = sealed_spec(cp, alice, iid)
        cp.platform = PlatformPolicy(env_admin=False, layout="single-machine", host_pattern="{component}-{id}.sid.example",
                                     region="fra", app_prefix="new")
        self.assertEqual(cp.reconfigure_instance(alice, iid, config=ISSUER)["status"], "running")
        after = sealed_spec(cp, alice, iid)
        for f in ("layout", "region", "app_prefix", "host_pattern", "env", "public_ips", "scale_to_zero", "env_admin"):
            self.assertEqual(getattr(after, f), getattr(before, f), f)
        self.assertNotIn(f"sid-{iid}", fake.apps, "no second, single-machine copy of the instance")
        self.assertFalse([a for a in fake.apps if a.startswith("new-")])

    def test_a_destroy_while_reconfiguring_wins(self):
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        cp.runner = DeferredRunner()
        self.assertEqual(cp.reconfigure_instance(alice, iid, config=ISSUER)["status"], "reconfiguring")
        cp.destroy_instance(alice, iid)
        cp.runner.run_all()
        self.assertEqual(cp.get_instance(alice, iid)["status"], "destroyed")

    def test_a_destroy_during_the_redeploy_wins(self):
        """The reaper (or the owner) destroys the instance while its redeploy is running:
        the finished job must not bring the row back to 'running'."""
        cp, fake, _ = make()
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        real = cp._deploy

        def deploy_then_destroyed(r, spec, sealer):
            result = real(r, spec, sealer)
            cp.destroy_instance(alice, iid)
            return result
        cp._deploy = deploy_then_destroyed
        cp.reconfigure_instance(alice, iid, config=ISSUER)
        self.assertEqual(cp.get_instance(alice, iid)["status"], "destroyed")
        self.assertEqual(cp.list_instances(alice), [])

    def test_reconfiguring_counts_as_live(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp, max_concurrent=1)
        iid = cp.create_instance(alice)["id"]
        cp.runner = DeferredRunner()
        cp.reconfigure_instance(alice, iid, config={})
        with self.assertRaises(QuotaExceeded):
            cp.create_instance(alice)


@NEEDS
class SingleMachineReconfigureTests(unittest.TestCase):
    def test_the_machine_config_is_updated_and_the_volume_stays(self):
        cp, fake, api, _, _, undo = make_single()
        self.addCleanup(undo)
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        m = api.only(f"sid-{iid}")
        updates, vols = m["updates"], [v["id"] for v in fake.apps[f"sid-{iid}"]["volumes"]]
        out = cp.reconfigure_instance(alice, iid, config=ISSUER)
        self.assertEqual(out["status"], "running", out["error"])
        self.assertEqual(api.only(f"sid-{iid}")["updates"], updates + 1, "one update = one restart of the machine")
        self.assertEqual([v["id"] for v in fake.apps[f"sid-{iid}"]["volumes"]], vols)
        self.assertEqual(sealed_spec(cp, alice, iid).layout, "single-machine")
        self.assertEqual(out["layout"], "single-machine")


@NEEDS
class HealthAndActivityTests(unittest.TestCase):
    def test_single_machine_health_reads_the_containers(self):
        cp, fake, api, _, _, undo = make_single()
        self.addCleanup(undo)
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        n = len(api.log)
        h = cp.instance_health(cp.principal_for(alice.user_id), iid)        # no unlock needed
        self.assertNotIn("error", h)
        self.assertEqual(h["instance"]["id"], iid)
        names = {c["name"] for c in h["components"]}
        self.assertIn("wallet-backend", names)
        self.assertTrue(all(c["healthy"] for c in h["components"]), h["components"])
        self.assertEqual({k for c in h["components"] for k in c}, {"name", "state", "healthy", "detail"})
        self.assertTrue(all(line.startswith("GET ") for line in api.log[n:]), "a probe only reads")
        self.assertLessEqual(len(api.log[n:]), 2)
        cp.stop_instance(alice, iid)
        h = cp.instance_health(alice, iid)
        self.assertTrue(h["components"] and not any(c["healthy"] for c in h["components"]))
        self.assertIn("stopped", h["components"][0]["detail"])

    def test_apps_layout_health_lists_each_component(self):
        from fakemachines import FakeMachines
        from sirosid_core.machines import MachinesClient
        cp, fake, _ = make()
        cp._machines = MachinesClient("tok", transport=FakeMachines(fake).transport, sleep=lambda s: None)
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        h = cp.instance_health(alice, iid)
        self.assertNotIn("error", h)
        names = [c["name"] for c in h["components"]]
        self.assertIn("mongodb", names)
        self.assertIn("wallet-frontend", names)
        self.assertNotIn("conformance", names, "a component the instance does not have is left out, not an error")
        self.assertTrue(all(c["healthy"] for c in h["components"]))

    def test_health_never_raises_on_a_fly_problem_and_never_carries_secrets(self):
        from fakemachines import FakeMachines
        from sirosid_core.machines import MachinesClient
        cp, fake, api, _, _, undo = make_single()
        self.addCleanup(undo)
        _, alice = admin_and_user(cp)
        iid = cp.create_instance(alice)["id"]
        token = cp.instance_credentials(alice, iid)["admin_token"]
        h = cp.instance_health(alice, iid)
        self.assertNotIn(token, json.dumps(h))
        self.assertNotIn("env", json.dumps(h["components"]))
        api.fail["GET /apps/"] = (500, {"error": "fly is having a day"})
        h = cp.instance_health(alice, iid)
        self.assertEqual(h["components"], [])
        self.assertIn("could not reach Fly", h["error"])
        api.fail.clear()

        def broken(*a, **k):
            raise OSError("network is unreachable")
        cp._machines = MachinesClient("tok", transport=broken, sleep=lambda s: self.fail("a probe must not retry"))
        h = cp.instance_health(alice, iid)
        self.assertEqual(h["components"], [])
        self.assertIn("unreachable", h["error"])

        class NoCredential:
            def __getattr__(self, name):
                raise RuntimeError("no token")
        cp._machines = NoCredential()
        self.assertEqual(cp.instance_health(alice, iid)["components"], [])

    def test_the_apps_probe_is_bounded_in_time(self):
        import time
        from sirosid_core.health import instance_health
        from sirosid_core.machines import MachinesClient

        def slow(*a, **k):
            time.sleep(1.0)
            return 200, b"[]"
        t0 = time.monotonic()
        r = instance_health(MachinesClient("tok", transport=slow), Naming("aaaaaaaa", app_prefix="sid"), timeout=0.1, budget=0.3)
        self.assertLess(time.monotonic() - t0, 0.9)
        self.assertIn("in time", r.error)

    def test_activity_is_the_instances_own_and_carries_no_secrets(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        iid = cp.create_instance(alice, config=ISSUER)["id"]
        cp.stop_instance(alice, iid)
        cp.start_instance(alice, iid)
        cp.reconfigure_instance(alice, iid, config={})
        bob = cp.begin_session(cp.redeem_invite(cp.create_invite(admin), "Bob").user_id, bytes(range(1, 33)))
        cp.save_config(bob, iid, {})                       # a config NAMED like alice's instance id
        cp.delete_config(bob, iid)
        acts = cp.instance_activity(alice, iid)
        self.assertEqual([a["action"] for a in acts][:4], ["reconfigured", "reconfigure_instance", "start_instance", "stop_instance"])
        self.assertEqual(acts[-1]["action"], "create_instance")
        self.assertNotIn("delete_config", [a["action"] for a in acts], "another user's audit row never shows")
        self.assertEqual({a["by"] for a in acts}, {"you", "system"})
        self.assertEqual(next(a for a in acts if a["action"] == "reconfigure_instance")["detail"]["changed"], ["trusted_issuers"])
        token = cp.instance_credentials(alice, iid)["admin_token"]
        dump = json.dumps(acts)
        self.assertNotIn(token, dump)
        self.assertNotIn("issuer.example.com", dump, "config values never reach the activity")
        self.assertEqual(len(cp.instance_activity(alice, iid, limit=2)), 2)
        cp.db.audit(alice.user_id, "stop_instance", iid, secret="hunter2")       # a detail key nobody vetted
        self.assertNotIn("hunter2", json.dumps(cp.instance_activity(alice, iid)))


class MigrationTests(unittest.TestCase):
    def test_an_old_database_gets_the_new_columns(self):
        import sqlite3
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = f"{d}/old.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE instances(id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,"
                  " naming TEXT NOT NULL, spec BLOB NOT NULL, config BLOB NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
                  " expires_at REAL, kept INTEGER NOT NULL DEFAULT 0, urls TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '')")
        c.execute("INSERT INTO instances(id,owner,status,naming,spec,config,created_at,updated_at) VALUES('a','u','running','{\"env\":\"a\"}',x'00',x'00',1,1)")
        c.commit()
        c.close()
        db = Database(path)
        self.addCleanup(db.close)
        r = db.one("SELECT * FROM instances")
        self.assertIsNone(r["config_name"])
        self.assertEqual(ControlPlane._public(r)["layout"], "apps")
        Database(path).close()                          # and again: idempotent


class StateStoreTests(unittest.TestCase):
    def test_state_round_trips_sealed_under_the_owners_key(self):
        from sirosid_service.db import SealedStateStore
        from sirosid_service.vault import Sealer
        db = Database()
        self.addCleanup(db.close)
        store = SealedStateStore(db, Sealer(KEY), "u1")
        store.save("i1", {"adminToken": b"secret-token", "vc-pki/rootCA.key": b"KEY"})
        self.assertEqual(store.load("i1")["adminToken"], b"secret-token")
        raw = bytes(db.one("SELECT data FROM state WHERE path='adminToken'")["data"])
        self.assertNotIn(b"secret-token", raw)


if __name__ == "__main__":
    unittest.main()
