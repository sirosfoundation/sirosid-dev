"""Passkey enrolment, login, web sessions and unlocking, against a software authenticator."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import cbor2  # noqa: F401
    import webauthn  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

if HAVE:
    from softauthn import SoftAuthenticator, b64u, unb64u
    from sirosid_service.auth import AuthConfig, AuthError, AuthService
    from sirosid_service.service import DAY, Forbidden, InvalidInvite
    from test_service import KEY, make

NEEDS = unittest.skipUnless(HAVE, "needs webauthn and cbor2 (sirosid_service/requirements.txt)")


def setup():
    cp, fake, clock = make()
    auth = AuthService(cp, AuthConfig(origins=("https://sirosid.dev",)))
    admin = cp.bootstrap_admin("Root")
    return cp, auth, admin, clock


def enroll(cp, auth, admin, authn=None, **invite):
    authn = authn or SoftAuthenticator()
    tok = cp.create_invite(admin, **invite)
    begin = auth.begin_enrollment(tok, "Alice", "alice@example.com")
    user, web = auth.finish_enrollment(begin["ceremony_id"], authn.create(begin["options"]))
    return authn, user, web, tok


def login(auth, authn, **kw):
    begin = auth.begin_login()
    return auth.finish_login(begin["ceremony_id"], authn.get(begin["options"], **kw))


@NEEDS
class EnrolmentTests(unittest.TestCase):
    def test_enrolment_creates_the_account_spends_the_invite_and_signs_in_locked(self):
        cp, auth, admin, _ = setup()
        authn, user, web, tok = enroll(cp, auth, admin)
        self.assertEqual(cp._user(user.user_id)["email"], "alice@example.com")
        self.assertEqual(user.session_id, "")
        who = auth.principal_from_token(web)
        self.assertEqual(who.user_id, user.user_id)
        self.assertEqual(who.session_id, "", "signed in but LOCKED until the key is handed over")
        with self.assertRaises(InvalidInvite):
            cp.check_invite(tok, "alice@example.com")
        self.assertEqual(len(auth.list_passkeys(user)), 1)

    def test_a_bad_invite_is_refused_before_any_ceremony(self):
        cp, auth, admin, _ = setup()
        with self.assertRaises(InvalidInvite):
            auth.begin_enrollment("nonsense", "A", "a@example.com")
        tok = cp.create_invite(admin, email="a@example.com")
        with self.assertRaises(InvalidInvite):
            auth.begin_enrollment(tok, "A", "b@example.com")

    def test_a_failed_ceremony_does_not_spend_the_invite(self):
        cp, auth, admin, _ = setup()
        tok = cp.create_invite(admin)
        begin = auth.begin_enrollment(tok, "A", "")
        evil = SoftAuthenticator().create(begin["options"], origin="https://evil.example")
        with self.assertRaises(AuthError):
            auth.finish_enrollment(begin["ceremony_id"], evil)
        cp.check_invite(tok, "")                                     # still valid
        begin = auth.begin_enrollment(tok, "A", "")
        auth.finish_enrollment(begin["ceremony_id"], SoftAuthenticator().create(begin["options"]))

    def test_the_options_demand_a_discoverable_credential_with_user_verification_and_ask_for_prf(self):
        cp, auth, admin, _ = setup()
        opts = auth.begin_enrollment(cp.create_invite(admin), "A", "")["options"]
        self.assertEqual(opts["rp"]["id"], "sirosid.dev")
        self.assertEqual(opts["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(opts["authenticatorSelection"]["userVerification"], "required")
        self.assertEqual(unb64u(opts["extensions"]["prf"]["eval"]["first"]), AuthConfig().prf_salt)
        self.assertEqual(len(AuthConfig().prf_salt), 32)

    def test_the_same_passkey_cannot_be_registered_twice(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        begin = auth.begin_add_passkey(user)
        again = authn.create(begin["options"], credential_id=authn.credential_ids[0])
        with self.assertRaises(AuthError):
            auth.finish_add_passkey(user, begin["ceremony_id"], again)


@NEEDS
class LoginTests(unittest.TestCase):
    def test_login_by_discoverable_credential(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        who, web = login(auth, authn)
        self.assertEqual(who.user_id, user.user_id)
        self.assertEqual(auth.principal_from_token(web).user_id, user.user_id)

    def test_ceremonies_are_single_use(self):
        cp, auth, admin, _ = setup()
        authn, _, _, _ = enroll(cp, auth, admin)
        begin = auth.begin_login()
        response = authn.get(begin["options"])
        auth.finish_login(begin["ceremony_id"], response)
        with self.assertRaises(AuthError):
            auth.finish_login(begin["ceremony_id"], response)

    def test_ceremonies_expire(self):
        cp, auth, admin, clock = setup()
        authn, _, _, _ = enroll(cp, auth, admin)
        begin = auth.begin_login()
        clock.advance(seconds=301)
        with self.assertRaises(AuthError):
            auth.finish_login(begin["ceremony_id"], authn.get(begin["options"]))

    def test_a_ceremony_of_the_wrong_kind_is_refused(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        begin = auth.begin_add_passkey(user)
        with self.assertRaises(AuthError):
            auth.finish_login(begin["ceremony_id"], authn.get(begin["options"]))

    def test_everything_wrong_looks_the_same(self):
        cp, auth, admin, _ = setup()
        authn, _, _, _ = enroll(cp, auth, admin)
        stranger = SoftAuthenticator()
        stranger.create(auth.begin_enrollment(cp.create_invite(admin), "S", "")["options"])
        messages = set()
        for kw, who in (({"origin": "https://evil.example"}, authn), ({"rp_id": "evil.example"}, authn),
                        ({"tamper_signature": True}, authn), ({"uv": False}, authn), ({}, stranger)):
            begin = auth.begin_login()
            try:
                auth.finish_login(begin["ceremony_id"], who.get(begin["options"], **kw))
                self.fail(f"accepted {kw}")
            except AuthError as e:
                messages.add(str(e))
        self.assertEqual(messages, {"the passkey could not be verified"}, "no oracle about WHY it failed")

    def test_a_replayed_assertion_with_a_rolled_back_counter_is_refused(self):
        cp, auth, admin, _ = setup()
        authn, _, _, _ = enroll(cp, auth, admin)
        login(auth, authn)
        login(auth, authn)
        begin = auth.begin_login()
        with self.assertRaises(AuthError):
            auth.finish_login(begin["ceremony_id"], authn.get(begin["options"], bump=-1))

    def test_a_disabled_account_cannot_sign_in(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        cp.disable_user(admin, user.user_id)
        with self.assertRaises(Forbidden):
            login(auth, authn)

    def test_failed_logins_are_audited(self):
        cp, auth, admin, _ = setup()
        authn, _, _, _ = enroll(cp, auth, admin)
        begin = auth.begin_login()
        with self.assertRaises(AuthError):
            auth.finish_login(begin["ceremony_id"], authn.get(begin["options"], tamper_signature=True))
        self.assertIn("login_refused", [r["action"] for r in cp.db.audit_log()])


@NEEDS
class WebSessionAndUnlockTests(unittest.TestCase):
    def test_unlocking_gives_the_principal_a_key_and_logout_takes_it_away(self):
        cp, auth, admin, _ = setup()
        authn, user, web, _ = enroll(cp, auth, admin)
        unlocked = auth.unlock(web, KEY)
        self.assertTrue(unlocked.session_id)
        again = auth.principal_from_token(web)
        self.assertEqual(again.session_id, unlocked.session_id)
        cp.save_config(again, "c", {"wallet_attestation": True})
        auth.logout(web)
        with self.assertRaises(Forbidden):
            auth.principal_from_token(web)
        with self.assertRaises(Exception):
            cp._sealer(unlocked)

    def test_one_users_token_cannot_unlock_another_users_data(self):
        cp, auth, admin, _ = setup()
        _, a, web_a, _ = enroll(cp, auth, admin)
        authn_b, b, web_b, _ = enroll(cp, auth, admin)
        auth.unlock(web_a, KEY)
        auth.unlock(web_b, bytes(range(1, 33)))
        self.assertNotEqual(auth.principal_from_token(web_a).session_id, auth.principal_from_token(web_b).session_id)

    def test_a_wrong_key_is_refused_on_a_later_login(self):
        cp, auth, admin, _ = setup()
        authn, _, web, _ = enroll(cp, auth, admin)
        auth.unlock(web, KEY)
        _, web2 = login(auth, authn)
        with self.assertRaises(Forbidden):
            auth.unlock(web2, bytes(range(1, 33)))
        auth.unlock(web2, KEY)

    def test_web_sessions_expire_and_tokens_are_stored_hashed(self):
        cp, auth, admin, clock = setup()
        _, _, web, _ = enroll(cp, auth, admin)
        self.assertNotIn(web, str(cp.db.all("SELECT * FROM web_sessions")))
        clock.advance(seconds=12 * 3600 + 1)
        with self.assertRaises(Forbidden):
            auth.principal_from_token(web)
        for bad in ("", None, "nonsense"):
            with self.assertRaises(Forbidden):
                auth.principal_from_token(bad)

    def test_disabling_a_user_ends_their_web_sessions_effect(self):
        cp, auth, admin, _ = setup()
        _, user, web, _ = enroll(cp, auth, admin)
        auth.unlock(web, KEY)
        cp.disable_user(admin, user.user_id)
        with self.assertRaises(Forbidden):
            auth.principal_from_token(web)

    def test_the_prf_workflow_end_to_end_with_a_software_passkey(self):
        """What the browser does: enrol, sign in, derive the PRF output locally with the
        advertised salt, build the main key from it, unlock. The server only ever sees
        the main key, never the PRF output, and the same passkey yields the same key."""
        import hashlib
        cp, auth, admin, _ = setup()
        authn, user, web, _ = enroll(cp, auth, admin)
        salt = unb64u(auth.begin_login()["options"]["extensions"]["prf"]["eval"]["first"])
        prf = authn.prf(authn.credential_ids[0], salt)
        main_key = hashlib.sha256(b"main-key|" + prf).digest()
        auth.unlock(web, main_key)
        cp.save_config(auth.principal_from_token(web), "mine", {"trusted_issuers": ["https://i.example.com"]})
        _, web2 = login(auth, authn)
        salt2 = unb64u(auth.begin_login()["options"]["extensions"]["prf"]["eval"]["first"])
        main_key2 = hashlib.sha256(b"main-key|" + authn.prf(authn.credential_ids[0], salt2)).digest()
        self.assertEqual(main_key, main_key2)
        who = auth.unlock(web2, main_key2)
        self.assertEqual(cp.get_config(who, "mine")["trusted_issuers"], ["https://i.example.com"])


@NEEDS
class PasskeyManagementTests(unittest.TestCase):
    def test_add_a_second_passkey_and_sign_in_with_it(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        second = SoftAuthenticator()
        begin = auth.begin_add_passkey(user)
        auth.finish_add_passkey(user, begin["ceremony_id"], second.create(begin["options"]), label="phone")
        self.assertEqual([p["label"] for p in auth.list_passkeys(user)], ["first passkey", "phone"])
        who, _ = login(auth, second)
        self.assertEqual(who.user_id, user.user_id)

    def test_the_only_passkey_cannot_be_removed(self):
        cp, auth, admin, _ = setup()
        _, user, _, _ = enroll(cp, auth, admin)
        with self.assertRaises(AuthError):
            auth.remove_passkey(user, auth.list_passkeys(user)[0]["id"])

    def test_a_removed_passkey_no_longer_signs_in(self):
        cp, auth, admin, _ = setup()
        authn, user, _, _ = enroll(cp, auth, admin)
        second = SoftAuthenticator()
        begin = auth.begin_add_passkey(user)
        auth.finish_add_passkey(user, begin["ceremony_id"], second.create(begin["options"]))
        auth.remove_passkey(user, b64u(authn.credential_ids[0]))
        with self.assertRaises(AuthError):
            login(auth, authn)
        login(auth, second)

    def test_one_user_cannot_remove_anothers_passkey(self):
        from sirosid_service.service import NotFound
        cp, auth, admin, _ = setup()
        _, a, _, _ = enroll(cp, auth, admin)
        _, b, _, _ = enroll(cp, auth, admin)
        with self.assertRaises(NotFound):
            auth.remove_passkey(b, auth.list_passkeys(a)[0]["id"])


if __name__ == "__main__":
    unittest.main()
