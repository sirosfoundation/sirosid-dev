"""Users' data is sealed under a key only their passkey can produce.

The claims that matter, each tested directly: a database dump holds no user data; an
admin cannot read it; work that needs only metadata (the reaper, the sweeper, stop,
start, destroy) still runs with nobody logged in; and work that needs the user's data
refuses without an unlocked session.
"""
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

if HAVE_CRYPTO:
    from sirosid_service.vault import Locked, Sealer, SessionKeys, VaultError, aad
    from sirosid_service.service import DAY, Forbidden, NotFound, ServiceError
    from test_service import KEY, Clock, admin_and_user, make

NEEDS_CRYPTO = unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography package (sirosid_service/requirements.txt)")
NEEDS_ALL = unittest.skipUnless(HAVE_CRYPTO and shutil.which("helm") and shutil.which("openssl"), "needs cryptography, helm, openssl")
SECRET_ISSUER = "https://very-distinctive-issuer.example.com"


@NEEDS_CRYPTO
class SealerTests(unittest.TestCase):
    def test_round_trip(self):
        s = Sealer(KEY)
        self.assertEqual(s.open(s.seal(b"hello", b"ctx"), b"ctx"), b"hello")

    def test_a_fresh_nonce_every_time(self):
        s = Sealer(KEY)
        self.assertNotEqual(s.seal(b"x", b"c"), s.seal(b"x", b"c"))

    def test_the_plaintext_is_not_in_the_blob(self):
        self.assertNotIn(b"hello-secret", Sealer(KEY).seal(b"hello-secret", b"c"))

    def test_wrong_key_wrong_context_and_tampering_all_fail(self):
        blob = Sealer(KEY).seal(b"data", aad("u1", "config", "x"))
        with self.assertRaises(VaultError):
            Sealer(bytes(range(1, 33))).open(blob, aad("u1", "config", "x"))
        for other in (aad("u2", "config", "x"), aad("u1", "spec", "x"), aad("u1", "config", "y")):
            with self.assertRaises(VaultError):
                Sealer(KEY).open(blob, other)
        flipped = bytearray(blob)
        flipped[-1] ^= 1
        with self.assertRaises(VaultError):
            Sealer(KEY).open(bytes(flipped), aad("u1", "config", "x"))

    def test_unknown_versions_and_short_blobs_are_refused(self):
        for bad in (b"", b"\x01short", b"\x09" + bytes(60)):
            with self.assertRaises(VaultError):
                Sealer(KEY).open(bad, b"c")

    def test_the_key_must_be_32_bytes(self):
        for k in (b"", b"short", bytes(31), bytes(33), "text"):
            with self.assertRaises(ValueError):
                Sealer(k)


@NEEDS_CRYPTO
class SessionKeysTests(unittest.TestCase):
    def test_keys_expire(self):
        clock = Clock()
        keys = SessionKeys(clock)
        sid = keys.put("u1", KEY, ttl=100)
        keys.get(sid, "u1")
        clock.advance(seconds=101)
        with self.assertRaises(Locked):
            keys.get(sid, "u1")

    def test_a_session_belongs_to_its_user(self):
        keys = SessionKeys(Clock())
        sid = keys.put("u1", KEY)
        with self.assertRaises(Locked):
            keys.get(sid, "u2")

    def test_unknown_empty_and_dropped_sessions_are_locked(self):
        keys = SessionKeys(Clock())
        sid = keys.put("u1", KEY)
        keys.drop(sid)
        for s in (sid, "", "nonsense", None):
            with self.assertRaises(Locked):
                keys.get(s, "u1")

    def test_the_lifetime_is_capped_and_must_be_positive(self):
        clock = Clock()
        keys = SessionKeys(clock)
        sid = keys.put("u1", KEY, ttl=10 * 365 * DAY)
        clock.advance(days=2)
        with self.assertRaises(Locked):
            keys.get(sid, "u1")
        with self.assertRaises(ValueError):
            keys.put("u1", KEY, ttl=0)

    def test_the_key_is_never_written_anywhere(self):
        keys = SessionKeys(Clock())
        keys.put("u1", KEY)
        self.assertEqual(len(keys), 1)
        self.assertFalse(any(KEY in str(v).encode() for v in vars(keys).values() if isinstance(v, (str, bytes))))


@NEEDS_CRYPTO
class UnlockTests(unittest.TestCase):
    def test_a_wrong_key_is_refused_after_the_first_unlock(self):
        cp, _, _ = make()
        admin = cp.bootstrap_admin("Root")
        alice = cp.redeem_invite(cp.create_invite(admin), "A")
        cp.begin_session(alice.user_id, KEY)
        with self.assertRaises(Forbidden):
            cp.begin_session(alice.user_id, bytes(range(1, 33)))
        cp.begin_session(alice.user_id, KEY)                       # the right key still works
        self.assertIn("unlock_refused", [r["action"] for r in cp.db.audit_log()])

    def test_a_disabled_user_cannot_unlock_and_loses_their_sessions(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        cp.disable_user(admin, alice.user_id)
        with self.assertRaises(Locked):
            cp._sealer(alice)
        with self.assertRaises(Forbidden):
            cp.begin_session(alice.user_id, KEY)

    def test_ending_a_session_locks_it(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        cp.save_config(alice, "c", {"wallet_attestation": True})
        cp.end_session(alice)
        with self.assertRaises(Locked):
            cp.get_config(alice, "c")

    def test_the_container_is_stored_opaque(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        self.assertIsNone(cp.get_privatedata(alice))
        blob = os.urandom(900)
        cp.set_privatedata(alice, blob)
        self.assertEqual(cp.get_privatedata(alice), blob)
        for bad in (b"", bytes(70 * 1024), "text"):
            with self.assertRaises(ServiceError):
                cp.set_privatedata(alice, bad)

    def test_the_container_is_readable_before_unlocking(self):
        """The browser needs it BEFORE it can unlock: that is how the passkey gets used."""
        cp, _, _ = make()
        admin = cp.bootstrap_admin("Root")
        alice = cp.redeem_invite(cp.create_invite(admin), "A")        # no session
        cp.set_privatedata(alice, b"wrapped-container")
        self.assertEqual(cp.get_privatedata(alice), b"wrapped-container")


@NEEDS_CRYPTO
class ConfigSealingTests(unittest.TestCase):
    def test_a_database_dump_holds_no_config(self):
        cp, _, _ = make()
        _, alice = admin_and_user(cp)
        cp.save_config(alice, "mine", {"trusted_issuers": [SECRET_ISSUER]})
        dump = b"".join(bytes(v) if isinstance(v, (bytes, bytearray)) else str(v).encode()
                        for t in ("configs", "audit", "users") for row in cp.db.all(f"SELECT * FROM {t}") for v in row.values())
        self.assertNotIn(SECRET_ISSUER.encode(), dump)
        self.assertEqual(cp.get_config(alice, "mine")["trusted_issuers"], [SECRET_ISSUER])

    def test_without_a_session_a_config_cannot_be_saved_or_read(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        cp.save_config(alice, "mine", {"wallet_attestation": True})
        locked = cp.principal_for(alice.user_id)
        with self.assertRaises(Locked):
            cp.get_config(locked, "mine")
        with self.assertRaises(Locked):
            cp.save_config(locked, "new", {})
        self.assertEqual([c["name"] for c in cp.list_configs(locked)], ["mine"], "names are metadata")

    def test_a_config_blob_cannot_be_moved_to_another_user_or_name(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        bob = cp.begin_session(cp.redeem_invite(cp.create_invite(admin), "Bob").user_id, bytes(range(1, 33)))
        cp.save_config(alice, "mine", {"wallet_attestation": True})
        row = cp.db.one("SELECT * FROM configs WHERE owner=?", (alice.user_id,))
        cp.db.execute("INSERT INTO configs(id,owner,name,doc,created_at,updated_at) VALUES('c_x',?,?,?,0,0)",
                      (bob.user_id, "stolen", row["doc"]))
        with self.assertRaises(VaultError):
            cp.get_config(bob, "stolen")

    def test_an_admin_cannot_read_a_users_config(self):
        cp, _, _ = make()
        admin, alice = admin_and_user(cp)
        cp.save_config(alice, "mine", {"wallet_attestation": True})
        admin = cp.begin_session(admin.user_id, bytes(range(2, 34)))
        with self.assertRaises(NotFound):
            cp.get_config(admin, "mine")


@NEEDS_ALL
class InstanceSealingTests(unittest.TestCase):
    def deployed(self):
        cp, fake, clock = make()
        admin, alice = admin_and_user(cp, max_kept=1, kept_for_days=30)
        iid = cp.create_instance(alice, config={"trusted_issuers": [SECRET_ISSUER]})["id"]
        return cp, fake, clock, admin, alice, iid

    def test_a_database_dump_holds_no_spec_and_no_instance_secret(self):
        cp, _, _, _, alice, iid = self.deployed()
        token = cp.instance_credentials(alice, iid)["admin_token"]
        self.assertEqual(len(token), 32)
        dump = b"".join(bytes(v) if isinstance(v, (bytes, bytearray)) else str(v).encode()
                        for t in ("instances", "state", "audit") for row in cp.db.all(f"SELECT * FROM {t}") for v in row.values())
        self.assertNotIn(SECRET_ISSUER.encode(), dump)
        self.assertNotIn(token.encode(), dump)
        self.assertNotIn(b"BEGIN", dump, "no PEM key material")

    def test_viewing_credentials_and_resetting_need_a_live_session(self):
        cp, _, _, _, alice, iid = self.deployed()
        locked = cp.principal_for(alice.user_id)
        with self.assertRaises(Locked):
            cp.instance_credentials(locked, iid)
        with self.assertRaises(Locked):
            cp.reset_instance(locked, iid)
        with self.assertRaises(Locked):
            cp.create_instance(locked)

    def test_everything_that_needs_only_metadata_works_with_nobody_logged_in(self):
        cp, fake, clock, admin, alice, iid = self.deployed()
        cp.end_session(alice)
        locked = cp.principal_for(alice.user_id)
        self.assertEqual(cp.list_instances(locked)[0]["id"], iid)
        self.assertEqual(cp.stop_instance(locked, iid)["status"], "stopped")
        self.assertEqual(cp.start_instance(locked, iid)["status"], "running")
        cp.set_keep(locked, iid, False)
        clock.advance(days=4)
        self.assertEqual(cp.reap(), [iid], "the reaper needs no key")
        self.assertEqual(fake.apps, {})

    def test_the_sweeper_and_destroy_need_no_key_either(self):
        cp, fake, clock, admin, alice, iid = self.deployed()
        cp.end_session(alice)
        self.assertEqual(cp.destroy_instance(cp.principal_for(alice.user_id), iid)["status"], "destroyed")
        self.assertEqual(cp.db.all("SELECT * FROM state"), [])
        self.assertEqual(cp.sweep_orphans()["orphans"], [])

    def test_reset_after_relocking_works_once_unlocked_again(self):
        cp, _, clock, _, alice, iid = self.deployed()
        token = cp.instance_credentials(alice, iid)["admin_token"]
        cp.end_session(alice)
        clock.advance(seconds=60)
        again = cp.begin_session(alice.user_id, KEY)
        self.assertEqual(cp.reset_instance(again, iid)["status"], "running")
        self.assertEqual(cp.instance_credentials(again, iid)["admin_token"], token)

    def test_a_different_users_key_cannot_open_someone_elses_instance_state(self):
        cp, _, _, admin, alice, iid = self.deployed()
        bob = cp.begin_session(cp.redeem_invite(cp.create_invite(admin), "Bob").user_id, bytes(range(1, 33)))
        with self.assertRaises(NotFound):
            cp.instance_credentials(bob, iid)

    def test_a_queued_deploy_finishes_after_the_session_that_queued_it_expires(self):
        """The job captures the requesting session's key; it is not re-derived later, so
        a slow deploy is not killed by the 8-hour clock - but nothing NEW can start."""
        class Deferred:
            def __init__(self):
                self.jobs = []

            def submit(self, fn, *args):
                self.jobs.append((fn, args))
        cp, _, clock = make()
        cp.runner = Deferred()
        _, alice = admin_and_user(cp)
        inst = cp.create_instance(alice)
        self.assertEqual(inst["status"], "creating")
        clock.advance(days=1)                                       # the session is long gone
        with self.assertRaises(Locked):
            cp.create_instance(alice)
        for fn, args in cp.runner.jobs:
            fn(*args)
        self.assertEqual(cp.get_instance(cp.principal_for(alice.user_id), inst["id"])["status"], "running")


if __name__ == "__main__":
    unittest.main()
