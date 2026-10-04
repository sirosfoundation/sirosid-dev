"""The HTTP API, end to end through a real ASGI app with a software passkey."""
import base64
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import cbor2  # noqa: F401
    import httpx  # noqa: F401
    import starlette  # noqa: F401
    import webauthn  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

if HAVE:
    from starlette.testclient import TestClient
    from softauthn import SoftAuthenticator
    from sirosid_service.api import COOKIE, ApiConfig, create_app
    from sirosid_service.auth import AuthConfig, AuthService
    from test_service import KEY, make

NEEDS = unittest.skipUnless(HAVE, "needs starlette, httpx, webauthn, cbor2 (sirosid_service/requirements.txt)")
NEEDS_HELM = unittest.skipUnless(HAVE and shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
ORIGIN = "https://sirosid.dev"
B64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Console:
    """A browser: a cookie jar, an Origin header, and a software passkey."""

    def __init__(self, app, authn=None, origin=ORIGIN):
        self.http = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
        self.authn = authn or SoftAuthenticator()
        self.origin = origin

    def req(self, method, path, json=None, origin=True, **kw):
        headers = dict(kw.pop("headers", {}))
        if origin:
            headers["Origin"] = origin if isinstance(origin, str) else self.origin
        return self.http.request(method, path, json=json, headers=headers, **kw)

    def post(self, path, json=None, **kw):
        return self.req("POST", path, json if json is not None else {}, **kw)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def enroll(self, invite, name="Alice", email="alice@example.com"):
        b = self.post("/api/enroll/begin", {"invite": invite, "name": name, "email": email}).json()
        return self.post("/api/enroll/finish", {"ceremony_id": b["ceremony_id"], "credential": self.authn.create(b["options"])})

    def login(self):
        b = self.post("/api/login/begin").json()
        return self.post("/api/login/finish", {"ceremony_id": b["ceremony_id"], "credential": self.authn.get(b["options"])})

    def unlock(self, key=None):
        return self.post("/api/unlock", {"main_key": B64(KEY if key is None else key)})


def build():
    cp, fake, clock = make()
    auth = AuthService(cp, AuthConfig(origins=(ORIGIN,)))
    app = create_app(cp, auth, ApiConfig(origins=(ORIGIN,)))
    admin = cp.bootstrap_admin("Root")
    return cp, auth, app, admin, fake, clock


def signed_in(cp, app, admin, unlocked=True, **invite):
    c = Console(app)
    r = c.enroll(cp.create_invite(admin, **invite))
    assert r.status_code == 201, r.text
    if unlocked:
        assert c.unlock().status_code == 200
    return c


@NEEDS
class AuthFlowTests(unittest.TestCase):
    def test_enrol_then_me_is_locked_then_unlock(self):
        cp, auth, app, admin, *_ = build()
        c = Console(app)
        r = c.enroll(cp.create_invite(admin))
        self.assertEqual(r.status_code, 201)
        me = c.get("/api/me").json()
        self.assertEqual((me["name"], me["role"], me["unlocked"]), ("Alice", "member", False))
        self.assertEqual(c.unlock().status_code, 200)
        self.assertTrue(c.get("/api/me").json()["unlocked"])

    def test_login_on_a_fresh_browser_and_logout(self):
        cp, auth, app, admin, *_ = build()
        first = Console(app)
        first.enroll(cp.create_invite(admin))
        second = Console(app, authn=first.authn)                  # same passkey, new cookie jar
        self.assertEqual(second.get("/api/me").status_code, 401)
        self.assertEqual(second.login().status_code, 200)
        self.assertEqual(second.get("/api/me").status_code, 200)
        self.assertEqual(second.post("/api/logout").status_code, 200)
        self.assertEqual(second.get("/api/me").status_code, 401)

    def test_the_key_container_round_trips_opaque(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin, unlocked=False)
        self.assertIsNone(c.get("/api/privatedata").json()["container"])
        blob = b"\x00wrapped\xff" * 40
        self.assertEqual(c.req("PUT", "/api/privatedata", {"container": B64(blob)}).status_code, 200)
        self.assertEqual(c.get("/api/privatedata").json()["container"], B64(blob))

    def test_a_bad_invite_is_a_400_and_does_not_reveal_which_way(self):
        cp, auth, app, admin, *_ = build()
        r = Console(app).post("/api/enroll/begin", {"invite": "nonsense", "name": "A"})
        self.assertEqual((r.status_code, r.json()["error"]), (400, "invalid_invite"))

    def test_a_forged_assertion_is_a_401_with_the_generic_message(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        b = c.post("/api/login/begin").json()
        r = c.post("/api/login/finish", {"ceremony_id": b["ceremony_id"],
                                           "credential": c.authn.get(b["options"], tamper_signature=True)})
        self.assertEqual((r.status_code, r.json()["message"]), (401, "the passkey could not be verified"))

    def test_passkey_management(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        b = c.post("/api/passkeys/begin").json()
        second = SoftAuthenticator()
        self.assertEqual(c.post("/api/passkeys/finish", {"ceremony_id": b["ceremony_id"], "credential": second.create(b["options"]),
                                                          "label": "phone"}).status_code, 200)
        ids = [p["id"] for p in c.get("/api/passkeys").json()["passkeys"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(c.req("DELETE", f"/api/passkeys/{ids[0]}").status_code, 200)
        self.assertEqual(c.req("DELETE", f"/api/passkeys/{ids[1]}").status_code, 401, "the last one stays")


@NEEDS
class WebSecurityTests(unittest.TestCase):
    def test_the_session_cookie_is_host_only_secure_httponly_and_strict(self):
        cp, auth, app, admin, *_ = build()
        c = Console(app)
        r = c.enroll(cp.create_invite(admin))
        cookie = r.headers["set-cookie"]
        self.assertTrue(cookie.startswith(f"{COOKIE}="), "__Host- prefix")
        low = cookie.lower()
        for needle in ("secure", "httponly", "samesite=strict", "path=/"):
            self.assertIn(needle, low)
        self.assertNotIn("domain=", low, "a Domain attribute would make it cross-subdomain")
        self.assertTrue(COOKIE.startswith("__Host-"))

    def test_state_changing_requests_need_an_allowed_origin(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        for origin in (False, "https://evil.example", "null", "http://sirosid.dev", "https://sirosid.dev.evil.example"):
            r = c.post("/api/configs/validate", {"config": {}}, origin=origin)
            self.assertEqual(r.status_code, 403, origin)
        self.assertEqual(c.post("/api/configs/validate", {"config": {}}).status_code, 200)

    def test_a_cookie_alone_never_authorises_a_write(self):
        """The CSRF case: the browser attaches the cookie to a cross-site POST."""
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        r = c.post("/api/instances", {"config": {}}, origin="https://evil.example")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.get("/api/instances").json()["instances"], [])

    def test_reads_do_not_need_an_origin_but_do_need_a_session(self):
        cp, auth, app, admin, *_ = build()
        self.assertEqual(Console(app).get("/api/instances").status_code, 401)
        c = signed_in(cp, app, admin)
        self.assertEqual(c.get("/api/instances", origin=False).status_code, 200)

    def test_every_response_carries_the_security_headers_and_is_not_cacheable(self):
        cp, auth, app, admin, *_ = build()
        for r in (Console(app).get("/healthz"), Console(app).get("/api/me"), Console(app).post("/api/login/begin")):
            h = r.headers
            self.assertIn("max-age=", h["strict-transport-security"])
            self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
            self.assertEqual(h["x-content-type-options"], "nosniff")
            self.assertEqual(h["cache-control"], "no-store")
            self.assertEqual(h["referrer-policy"], "no-referrer")

    def test_bodies_must_be_small_json_objects(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        self.assertEqual(c.req("POST", "/api/configs/validate", None, content=b"x" * 300_000,
                               headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(c.req("POST", "/api/configs/validate", None, content=b"[1,2]",
                               headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(c.req("POST", "/api/configs/validate", None, content=b"{bad",
                               headers={"content-type": "application/json"}).status_code, 400)
        self.assertEqual(c.req("POST", "/api/configs/validate", None, content=b'{"config": {}}',
                               headers={"content-type": "text/plain"}).status_code, 400, "form posts are not JSON")

    def test_guessing_invites_is_rate_limited(self):
        cp, auth, app, admin, *_ = build()
        c = Console(app)
        codes = [c.post("/api/enroll/begin", {"invite": f"guess{i}", "name": "A"}).status_code for i in range(12)]
        self.assertEqual(codes[:8], [400] * 8)
        self.assertIn(429, codes[8:])

    def test_the_limit_is_per_client_not_global(self):
        cp, auth, app, admin, *_ = build()
        a = Console(app)
        for i in range(10):
            a.post("/api/enroll/begin", {"invite": f"g{i}", "name": "A"})
        b = Console(app)
        b.http = TestClient(app, base_url=ORIGIN, client=("203.0.113.9", 5000), raise_server_exceptions=False)
        self.assertEqual(b.post("/api/enroll/begin", {"invite": "x", "name": "A"}).status_code, 400)

    def test_errors_do_not_leak_internals(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        r = c.get("/api/instances/does-not-exist")
        self.assertEqual((r.status_code, r.json()["error"]), (404, "not_found"))
        self.assertNotIn("Traceback", r.text)
        self.assertEqual(c.req("PUT", "/api/privatedata", {"container": "!!!"}).status_code, 400)

    def test_an_unknown_route_and_method_do_not_crash(self):
        cp, auth, app, admin, *_ = build()
        self.assertEqual(Console(app).get("/api/nope").status_code, 404)
        self.assertEqual(Console(app).req("PATCH", "/api/me").status_code, 405)


@NEEDS_HELM
class ControlPlaneThroughHttpTests(unittest.TestCase):
    def test_the_whole_lifecycle_over_http(self):
        cp, auth, app, admin, fake, clock = build()
        c = signed_in(cp, app, admin)
        self.assertEqual(c.req("PUT", "/api/configs/mine", {"config": {"trusted_issuers": ["https://i.example.com"]}}).status_code, 200)
        self.assertEqual(c.get("/api/configs/mine").json()["config"]["trusted_issuers"], ["https://i.example.com"])
        r = c.post("/api/instances", {"config_name": "mine", "name": "demo"})
        self.assertEqual(r.status_code, 202, r.text)
        inst = r.json()
        self.assertEqual(inst["status"], "running")
        iid = inst["id"]
        self.assertEqual(len(c.get(f"/api/instances/{iid}/credentials").json()["admin_token"]), 32)
        self.assertEqual(c.post(f"/api/instances/{iid}/stop").json()["status"], "stopped")
        self.assertEqual(c.post(f"/api/instances/{iid}/start").json()["status"], "running")
        self.assertEqual(c.post(f"/api/instances/{iid}/reset").status_code, 202)
        self.assertEqual(c.req("DELETE", f"/api/instances/{iid}").json()["status"], "destroyed")
        self.assertEqual(c.get("/api/instances").json()["instances"], [])
        self.assertEqual(fake.apps, {})

    def test_a_locked_session_gets_423_for_anything_touching_its_data(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin, unlocked=False)
        for method, path, body in (("POST", "/api/instances", {"config": {}}), ("PUT", "/api/configs/x", {"config": {}})):
            r = c.req(method, path, body)
            self.assertEqual((r.status_code, r.json()["error"]), (423, "locked"), path)
        self.assertEqual(c.get("/api/instances").status_code, 200, "metadata needs no key")

    def test_policy_problems_come_back_all_at_once_as_a_422(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        r = c.req("PUT", "/api/configs/x", {"config": {"trusted_issuers": ["http://x"], "images": {"pdp": "ghcr.io/a/b:1"}, "mystery": 1}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(sorted(p["path"] for p in r.json()["problems"]), ["images", "mystery", "trusted_issuers[0]"])

    def test_users_cannot_reach_each_others_instances(self):
        cp, auth, app, admin, *_ = build()
        a, b = signed_in(cp, app, admin), signed_in(cp, app, admin)
        iid = a.post("/api/instances", {"config": {}}).json()["id"]
        for method, path in (("GET", f"/api/instances/{iid}"), ("GET", f"/api/instances/{iid}/credentials"),
                             ("POST", f"/api/instances/{iid}/stop"), ("DELETE", f"/api/instances/{iid}")):
            self.assertEqual(b.req(method, path).status_code, 404, path)

    def test_quota_is_a_409(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin, max_concurrent=1)
        c.post("/api/instances", {"config": {}})
        r = c.post("/api/instances", {"config": {}})
        self.assertEqual((r.status_code, r.json()["error"]), (409, "quota"))


@NEEDS
class AdminTests(unittest.TestCase):
    def admin_console(self, cp, app, admin):
        c = Console(app)
        c.enroll(cp.create_invite(admin, role="admin"))
        c.unlock()
        return c

    def test_a_member_gets_a_plain_404_on_admin_routes(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        for method, path in (("POST", "/api/admin/invites"), ("GET", "/api/admin/invites"), ("GET", "/api/admin/audit"),
                             ("GET", "/api/admin/instances")):
            r = c.req(method, path, {} if method == "POST" else None)
            self.assertEqual(r.status_code, 404, path)

    def test_an_admin_issues_a_working_invite_over_http(self):
        cp, auth, app, admin, *_ = build()
        boss = self.admin_console(cp, app, admin)
        r = boss.post("/api/admin/invites", {"capabilities": ["custom_images"], "max_concurrent": 3, "days_valid": 2})
        self.assertEqual(r.status_code, 200, r.text)
        newbie = Console(app)
        self.assertEqual(newbie.enroll(r.json()["token"], "Dev", "dev@example.com").status_code, 201)
        self.assertIn("custom_images", newbie.get("/api/me").json()["capabilities"])
        self.assertEqual(len(boss.get("/api/admin/invites").json()["invites"]), 2, "the admin's own invite plus this one")
        self.assertGreater(len(boss.get("/api/admin/audit?limit=5").json()["audit"]), 0)

    def test_admin_actions_need_the_origin_check_like_everything_else(self):
        cp, auth, app, admin, *_ = build()
        boss = self.admin_console(cp, app, admin)
        self.assertEqual(boss.post("/api/admin/invites", {}, origin="https://evil.example").status_code, 403)

    def test_disabling_a_user_over_http_locks_them_out(self):
        cp, auth, app, admin, *_ = build()
        boss = self.admin_console(cp, app, admin)
        c = signed_in(cp, app, admin)
        uid = c.get("/api/me").json()["user_id"]
        self.assertEqual(boss.post(f"/api/admin/users/{uid}/disable").status_code, 200)
        r = c.get("/api/me")
        self.assertEqual((r.status_code, r.json()["message"]), (403, "this account is disabled"))


if __name__ == "__main__":
    unittest.main()
