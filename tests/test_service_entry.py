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

    def test_the_sweep_grace_is_configurable_but_never_near_zero(self):
        from sirosid_service.__main__ import build
        s = settings(SIROSID_SWEEP_GRACE_SECONDS="120")
        cp, _, _ = build(s)
        self.assertEqual(cp.limits.sweep_grace_seconds, 120.0)
        self.assertEqual(settings().sweep_grace_seconds, 3600.0)
        for bad in ("0", "59"):
            with self.assertRaises(SystemExit):
                settings(SIROSID_SWEEP_GRACE_SECONDS=bad)
        with self.assertRaises(SystemExit):
            settings(SIROSID_TICK_SECONDS="0")


@NEEDS
class DeploymentFacingTests(unittest.TestCase):
    def test_a_database_on_disk_is_in_wal_mode_for_litestream(self):
        import tempfile
        from sirosid_service.db import Database
        d = tempfile.mkdtemp()
        db = Database(os.path.join(d, "s.db"))
        self.addCleanup(db.close)
        self.assertEqual(db.one("PRAGMA journal_mode")["journal_mode"], "wal")
        self.assertGreaterEqual(db.one("PRAGMA busy_timeout")["timeout"], 1000)

    def test_the_console_host_serves_no_webauthn_related_origins_file(self):
        """Related Origin Requests: a host serving /.well-known/webauthn can let OTHER
        origins use its host as their RP ID. The console host must never serve one."""
        cp, fake, clock = make()
        auth = AuthService(cp, AuthConfig(origins=(ORIGIN,)))
        c = Console(create_app(cp, auth, ApiConfig(origins=(ORIGIN,))))
        for path in ("/.well-known/webauthn", "/.well-known/webauthn/", "/.well-known/passkey-endpoints"):
            self.assertEqual(c.get(path).status_code, 404, path)


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
