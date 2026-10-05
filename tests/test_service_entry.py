import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import cryptography, starlette, webauthn  # noqa: F401,E401
    HAVE = True
except ImportError:
    HAVE = False
if HAVE:
    from sirosid_service.config import Settings
    from sirosid_service.service import Forbidden
    from test_api import Console, ORIGIN
    from test_service import make
    from sirosid_service.api import ApiConfig, create_app
    from sirosid_service.auth import AuthConfig, AuthService

NEEDS = unittest.skipUnless(HAVE, "needs the service dependencies")


def settings(**env):
    base = {"FLY_API_TOKEN": "tok", "SIROSID_DB": ":memory:"}
    base.update(env)
    old = {k: os.environ.get(k) for k in base}
    os.environ.update(base)
    try:
        return Settings.from_env()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@NEEDS
class ConfigTests(unittest.TestCase):
    def test_defaults_describe_the_planned_deployment(self):
        s = settings()
        self.assertEqual((s.rp_id, s.origins, s.fly_org, s.app_prefix), ("sirosid.dev", ("https://sirosid.dev",), "sirosdev", "sid"))

    def test_the_assistant_is_off_without_a_key_and_auto_routed_with_one(self):
        self.assertEqual(settings().chat_models, ())
        self.assertEqual(settings(OPENROUTER_API_KEY="k").chat_models, ("openrouter/auto",))
        self.assertEqual(settings(OPENROUTER_API_KEY="k", SIROSID_CHAT_MODELS="a/b, c/d").chat_models, ("a/b", "c/d"))
        with self.assertRaises(SystemExit):
            settings(SIROSID_CHAT_MODELS="a/b")                     # models without a key

    def test_a_token_is_required(self):
        old = os.environ.pop("FLY_API_TOKEN", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                Settings.from_env()
            self.assertIn("FLY_API_TOKEN", str(cm.exception))
        finally:
            if old is not None:
                os.environ["FLY_API_TOKEN"] = old

    def test_origins_must_be_https_and_under_the_rp_id(self):
        for origin in ("http://sirosid.dev", "https://evil.example", "https://sirosid.dev.evil.example"):
            with self.assertRaises(SystemExit, msg=origin):
                settings(SIROSID_ORIGINS=origin)
        self.assertEqual(settings(SIROSID_ORIGINS="https://console.sirosid.dev").origins, ("https://console.sirosid.dev",))

    def test_a_host_pattern_must_vary_per_instance(self):
        with self.assertRaises(SystemExit):
            settings(SIROSID_HOST_PATTERN="shared.example.com")


@NEEDS
class BootstrapInviteTests(unittest.TestCase):
    def test_the_first_admin_enrols_with_a_bootstrap_invite_and_it_cannot_be_reused_to_mint_more(self):
        cp, fake, clock = make()
        token = cp.bootstrap_invite()
        auth = AuthService(cp, AuthConfig(origins=(ORIGIN,)))
        c = Console(create_app(cp, auth, ApiConfig(origins=(ORIGIN,))))
        self.assertEqual(c.enroll(token, "Root", "root@example.com").status_code, 201)
        me = c.get("/api/me").json()
        self.assertEqual(me["role"], "admin")
        self.assertIn("custom_images", me["capabilities"])
        with self.assertRaises(Forbidden):
            cp.bootstrap_invite()

    def test_it_expires_and_is_audited(self):
        cp, fake, clock = make()
        token = cp.bootstrap_invite(days_valid=1)
        clock.advance(days=2)
        from sirosid_service.service import InvalidInvite
        with self.assertRaises(InvalidInvite):
            cp.check_invite(token)
        self.assertIn("bootstrap_invite", [r["action"] for r in cp.db.audit_log()])


if __name__ == "__main__":
    unittest.main()
