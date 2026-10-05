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
