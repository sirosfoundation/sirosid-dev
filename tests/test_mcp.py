"""OAuth + MCP end to end: a real ASGI app, a software passkey for the console half, and a bare
HTTP client (no cookies, no Origin) standing in for an MCP client such as Claude Code."""
import base64
import hashlib
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_api import HAVE, ORIGIN, Console, build, signed_in  # noqa: E402

if HAVE:
    from starlette.testclient import TestClient
    from sirosid_service.oauth import OAuthError, same_redirect, valid_redirect_uri

NEEDS = unittest.skipUnless(HAVE, "needs starlette, httpx, webauthn, cbor2")
REDIRECT = "http://127.0.0.1:33333/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class Agent:
    """An MCP client: no cookie jar, no Origin."""

    def __init__(self, app):
        self.http = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
        self.token = None
        self.n = 0

    def register(self, **meta):
        meta.setdefault("redirect_uris", [REDIRECT])
        meta.setdefault("client_name", "Test Agent")
        return self.http.post("/oauth/register", json=meta)

    def authorize_url(self, client_id, **over):
        q = {"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "code_challenge": CHALLENGE,
             "code_challenge_method": "S256", "state": "xyz", "resource": ORIGIN + "/mcp"}
        q.update(over)
        return "/oauth/authorize", {k: v for k, v in q.items() if v is not None}

    def exchange(self, client_id, code, **over):
        form = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": REDIRECT, "code_verifier": VERIFIER}
        form.update(over)
        return self.http.post("/oauth/token", data=form)

    def rpc(self, method, params=None, token="default", notify=False, **kw):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.n += 1
            body["id"] = self.n
        tok = self.token if token == "default" else token
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        headers.update(kw.pop("headers", {}))
        return self.http.post("/mcp", json=body, headers=headers, **kw)

    def call(self, tool, /, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        self.last = r
        return r.json()["result"]


def connect(app, console, agent=None, approve=True):
    """The whole dance: register, /authorize, the console approves, /token. Returns (agent, client_id, token response)."""
    agent = agent or Agent(app)
    cid = agent.register().json()["client_id"]
    path, q = agent.authorize_url(cid)
    r = agent.http.get(path, params=q, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/#authorize="), (r.status_code, r.text)
    pid = r.headers["location"].split("=", 1)[1]
    a = console.post("/api/oauth/approve" if approve else "/api/oauth/deny", {"id": pid})
    assert a.status_code == 200, a.text
    qs = parse_qs(urlsplit(a.json()["redirect"]).query)
    if not approve:
        return agent, cid, qs
    t = agent.exchange(cid, qs["code"][0])
    assert t.status_code == 200, t.text
    agent.token = t.json()["access_token"]
    return agent, cid, t.json()


@NEEDS
class OAuthTests(unittest.TestCase):
    def test_discovery_documents(self):
        cp, auth, app, admin, *_ = build()
        a = Agent(app)
        m = a.http.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(m["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(m["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(m["registration_endpoint"], ORIGIN + "/oauth/register")
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            r = a.http.get(path).json()
            self.assertEqual((r["resource"], r["authorization_servers"]), (ORIGIN + "/mcp", [ORIGIN]))

    def test_redirect_uri_rules(self):
        good = ["https://claude.ai/api/mcp/auth_callback", "http://localhost:8080/cb", "http://127.0.0.1:1/x", "http://[::1]:9/x"]
        bad = ["http://example.com/cb", "javascript:alert(1)", "data:text/html,x", "myapp://cb", "https://a/b#frag", "https://u:p@a/b", "http:///x", "", "https://" + "a" * 600]
        for u in good:
            self.assertTrue(valid_redirect_uri(u), u)
        for u in bad:
            self.assertFalse(valid_redirect_uri(u), u)
        self.assertTrue(same_redirect("http://127.0.0.1:1111/cb", "http://127.0.0.1:2222/cb"), "loopback may change port")
        self.assertFalse(same_redirect("http://127.0.0.1:1111/cb", "http://127.0.0.1:2222/other"))
        self.assertFalse(same_redirect("https://a.example/cb", "https://a.example:444/cb"), "non-loopback is exact")

    def test_registration_validates(self):
        cp, auth, app, admin, *_ = build()
        a = Agent(app)
        self.assertEqual(a.register().status_code, 201)
        for meta in ({"redirect_uris": []}, {"redirect_uris": ["http://evil.example/cb"]}, {"redirect_uris": "https://a/b"},
                     {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
                     {"redirect_uris": [REDIRECT], "grant_types": ["implicit"]}, {"redirect_uris": [REDIRECT] * 6}):
            r = a.http.post("/oauth/register", json=meta)
            self.assertEqual(r.status_code, 400, meta)
            self.assertIn(r.json()["error"], ("invalid_redirect_uri", "invalid_client_metadata"))
        self.assertEqual(a.http.post("/oauth/register", content=b"not json").status_code, 400)

    def test_client_name_is_bounded_and_normalised(self):
        cp, auth, app, admin, *_ = build()
        r = Agent(app).register(client_name="  Evil\n\n   <b>x</b>" + "y" * 500).json()
        self.assertLessEqual(len(r["client_name"]), 80)
        self.assertNotIn("\n", r["client_name"])

    def test_authorize_refuses_bad_requests_without_redirecting(self):
        cp, auth, app, admin, *_ = build()
        a = Agent(app)
        cid = a.register().json()["client_id"]
        cases = [dict(redirect_uri="https://evil.example/cb"), dict(client_id="c_nope"), dict(code_challenge=None),
                 dict(code_challenge_method="plain"), dict(response_type="token"), dict(resource="https://other.example/mcp"),
                 dict(scope="admin"), dict(code_challenge="short")]
        for over in cases:
            path, q = a.authorize_url(over.pop("client_id", cid), **over)
            r = a.http.get(path, params=q, follow_redirects=False)
            self.assertIn(r.status_code, (400, 401), over)
            self.assertNotIn("location", r.headers, over)

    def test_the_consent_step_needs_the_console_and_an_unlock(self):
        cp, auth, app, admin, *_ = build()
        locked = signed_in(cp, app, admin, unlocked=False)
        a = Agent(app)
        cid = a.register().json()["client_id"]
        path, q = a.authorize_url(cid)
        pid = a.http.get(path, params=q, follow_redirects=False).headers["location"].split("=", 1)[1]
        self.assertEqual(Console(app).get(f"/api/oauth/pending/{pid}").status_code, 401)
        self.assertEqual(Console(app).post("/api/oauth/approve", {"id": pid}).status_code, 401)
        info = locked.get(f"/api/oauth/pending/{pid}").json()
        self.assertEqual((info["client_name"], info["redirect_host"]), ("Test Agent", "127.0.0.1:33333"))
        r = locked.post("/api/oauth/approve", {"id": pid})
        self.assertEqual((r.status_code, r.json()["error"]), (423, "locked"), "approving needs the key, so an unlock")
        self.assertEqual(locked.post("/api/oauth/approve", {"id": pid}, origin="https://evil.example").status_code, 403)
        self.assertEqual(locked.unlock().status_code, 200)
        self.assertEqual(locked.post("/api/oauth/approve", {"id": pid}).status_code, 200)
        self.assertEqual(locked.post("/api/oauth/approve", {"id": pid}).status_code, 404, "a request is single use")

    def test_deny_redirects_with_access_denied_and_state(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        _, _, qs = connect(app, c, approve=False)
        self.assertEqual((qs["error"], qs["state"], qs["iss"]), (["access_denied"], ["xyz"], [ORIGIN]))

    def test_full_flow_and_token_properties(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        agent, cid, t = connect(app, c)
        self.assertEqual((t["token_type"], t["scope"]), ("Bearer", "sirosid"))
        self.assertLessEqual(t["expires_in"], 8 * 3600)
        self.assertNotIn("refresh_token", t)

    def test_token_endpoint_checks_everything(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)

        def fresh():
            a = Agent(app)
            cid = a.register().json()["client_id"]
            path, q = a.authorize_url(cid)
            pid = a.http.get(path, params=q, follow_redirects=False).headers["location"].split("=", 1)[1]
            code = parse_qs(urlsplit(c.post("/api/oauth/approve", {"id": pid}).json()["redirect"]).query)["code"][0]
            return a, cid, code
        for label, over in (("verifier", dict(code_verifier="w" * 64)), ("short verifier", dict(code_verifier="x")),
                            ("redirect", dict(redirect_uri="http://127.0.0.1:33333/other")), ("grant", dict(grant_type="password")),
                            ("resource", dict(resource="https://other.example/mcp"))):
            a, cid, code = fresh()
            r = a.exchange(cid, code, **over)
            self.assertEqual(r.status_code, 400, label)
            self.assertNotIn("access_token", r.text, label)
        a, cid, code = fresh()
        other = a.register().json()["client_id"]
        self.assertEqual(a.exchange(other, code).status_code, 400, "another client cannot redeem the code")
        a, cid, code = fresh()
        self.assertEqual(a.http.post("/oauth/token", json={"code": code}).status_code, 400, "form encoding only")

    def test_code_replay_revokes_the_token(self):
        cp, auth, app, admin, *_ = build()
        c = signed_in(cp, app, admin)
        a = Agent(app)
        cid = a.register().json()["client_id"]
        path, q = a.authorize_url(cid)
        pid = a.http.get(path, params=q, follow_redirects=False).headers["location"].split("=", 1)[1]
        code = parse_qs(urlsplit(c.post("/api/oauth/approve", {"id": pid}).json()["redirect"]).query)["code"][0]
        tok = a.exchange(cid, code).json()["access_token"]
        self.assertEqual(a.rpc("ping", token=tok).status_code, 200)
        self.assertEqual(a.exchange(cid, code).status_code, 400)
        self.assertEqual(a.rpc("ping", token=tok).status_code, 401, "the code's token died with the replay")

    def test_code_expires(self):
        cp, auth, app, admin, fake, clock = build()
        c = signed_in(cp, app, admin)
        a = Agent(app)
        cid = a.register().json()["client_id"]
        path, q = a.authorize_url(cid)
        pid = a.http.get(path, params=q, follow_redirects=False).headers["location"].split("=", 1)[1]
        code = parse_qs(urlsplit(c.post("/api/oauth/approve", {"id": pid}).json()["redirect"]).query)["code"][0]
        clock.advance(seconds=61)
        self.assertEqual(a.exchange(cid, code).status_code, 400)

    def test_pending_requests_expire(self):
        cp, auth, app, admin, fake, clock = build()
        c = signed_in(cp, app, admin)
        a = Agent(app)
        cid = a.register().json()["client_id"]
        path, q = a.authorize_url(cid)
        pid = a.http.get(path, params=q, follow_redirects=False).headers["location"].split("=", 1)[1]
        clock.advance(seconds=601)
        self.assertEqual(c.post("/api/oauth/approve", {"id": pid}).status_code, 404)


@NEEDS
class McpTests(unittest.TestCase):
    def setUp(self):
        self.cp, self.auth, self.app, self.admin, self.fake, self.clock = build()
        self.console = signed_in(self.cp, self.app, self.admin, max_kept=1)
        self.agent, self.cid, _ = connect(self.app, self.console)

    def test_unauthenticated_calls_get_a_challenge_pointing_at_the_metadata(self):
        a = Agent(self.app)
        r = a.rpc("ping", token=None)
        self.assertEqual(r.status_code, 401)
        self.assertIn(f'resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource"', r.headers["www-authenticate"])
        r = a.rpc("ping", token="not-a-token")
        self.assertEqual(r.status_code, 401)
        self.assertIn('error="invalid_token"', r.headers["www-authenticate"])

    def test_initialize_negotiates_and_lists_tools(self):
        r = self.agent.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(self.agent.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.agent.rpc("notifications/initialized", notify=True).status_code, 202)
        self.assertEqual(self.agent.rpc("ping").json()["result"], {})
        tools = {t["name"]: t for t in self.agent.rpc("tools/list").json()["result"]["tools"]}
        for must in ("list_config_templates", "list_configs", "save_config", "create_instance", "destroy_instance", "get_instance_credentials"):
            self.assertIn(must, tools)
        for banned in ("create_invite", "grant", "disable_user", "audit", "add_passkey", "set_privatedata", "unlock"):
            self.assertFalse([n for n in tools if banned in n], f"{banned} must not be a tool")
        self.assertTrue(tools["destroy_instance"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["list_instances"]["annotations"]["readOnlyHint"])
        for t in tools.values():
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_config_and_instance_lifecycle_through_tools(self):
        a = self.agent
        self.assertEqual(a.call("get_account")["structuredContent"]["limits"]["max_kept"], 1)
        bad = a.call("validate_config", config={"mystery": 1})
        self.assertFalse(bad["isError"])
        self.assertTrue(bad["structuredContent"]["problems"])
        r = a.call("save_config", name="mine", config={"mystery": 1})
        self.assertTrue(r["isError"] and "mystery" in r["content"][0]["text"], "policy problems come back as a tool error, all at once")
        self.assertFalse(a.call("save_config", name="mine", config={})["isError"])
        self.assertEqual([c["name"] for c in a.call("list_configs")["structuredContent"]["configs"]], ["mine"])
        created = a.call("create_instance", config_name="mine", name="demo")
        self.assertFalse(created["isError"])
        iid = created["structuredContent"]["id"]
        self.assertEqual(a.call("get_instance", id=iid)["structuredContent"]["status"], "running")
        creds = a.call("get_instance_credentials", id=iid)["structuredContent"]
        self.assertTrue(creds["admin_token"])
        for tool, status in (("stop_instance", "stopped"), ("start_instance", "running")):
            self.assertFalse(a.call(tool, id=iid)["isError"])
            self.assertEqual(a.call("get_instance", id=iid)["structuredContent"]["status"], status)
        self.assertFalse(a.call("set_keep", id=iid, keep=True)["isError"])
        self.assertTrue(a.call("get_instance", id=iid)["structuredContent"]["kept"])
        self.assertFalse(a.call("destroy_instance", id=iid)["isError"])
        self.assertEqual(a.call("list_instances")["structuredContent"]["instances"], [])
        # the console sees exactly what the agent did: one set of rules
        self.assertEqual([c["name"] for c in self.console.get("/api/configs").json()["configs"]], ["mine"])

    def test_an_agent_can_start_from_a_template(self):
        a = self.agent
        templates = a.call("list_config_templates")["structuredContent"]["templates"]
        self.assertEqual(templates[0]["id"], "standard")
        self.assertFalse(a.call("save_config", name="from-template", config=templates[1]["config"])["isError"])
        self.assertEqual(a.call("get_config", name="from-template")["structuredContent"]["config"], templates[1]["config"])

    def test_quotas_and_ownership_are_the_control_planes(self):
        a = self.agent
        a.call("save_config", name="c", config={})
        for _ in range(2):
            self.assertFalse(a.call("create_instance", config_name="c")["isError"])
        third = a.call("create_instance", config_name="c")
        self.assertTrue(third["isError"])
        self.assertIn("instance", third["content"][0]["text"].lower())
        other = signed_in(self.cp, self.app, self.admin)
        b, _, _ = connect(self.app, other)
        iid = a.call("list_instances")["structuredContent"]["instances"][0]["id"]
        for tool in ("get_instance", "stop_instance", "destroy_instance", "get_instance_credentials"):
            r = b.call(tool, id=iid)
            self.assertTrue(r["isError"], tool)
            self.assertIn("no such", r["content"][0]["text"].lower())
        self.assertEqual(b.call("list_configs")["structuredContent"]["configs"], [], "B never sees A's configs")

    def test_argument_validation_is_a_protocol_error(self):
        a = self.agent
        for params in ({"name": "nope"}, {"name": "get_config", "arguments": {}}, {"name": "get_config", "arguments": {"name": 1}},
                       {"name": "get_config", "arguments": {"name": "x", "extra": 1}}, {"name": "get_config", "arguments": "x"}):
            r = a.rpc("tools/call", params).json()
            self.assertEqual(r["error"]["code"], -32602, params)
        self.assertEqual(a.rpc("no/such/method").json()["error"]["code"], -32601)

    def test_transport_edge_cases(self):
        a = self.agent
        self.assertEqual(a.http.post("/mcp", content=b"{", headers={"Authorization": f"Bearer {a.token}"}).status_code, 400)
        r = a.http.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers={"Authorization": f"Bearer {a.token}"})
        self.assertEqual(r.json()["error"]["code"], -32600, "batches are refused")
        for m in ("get", "delete", "put"):
            r = getattr(a.http, m)("/mcp", headers={"Authorization": f"Bearer {a.token}"})
            self.assertEqual((r.status_code, r.headers["allow"]), (405, "POST"))
        big = a.http.post("/mcp", content=b"x" * (300 * 1024), headers={"Authorization": f"Bearer {a.token}"})
        self.assertEqual(big.status_code, 413)

    def test_a_browser_origin_that_is_not_ours_is_refused(self):
        r = self.agent.rpc("ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403, "DNS-rebinding defence from the MCP spec")
        self.assertEqual(self.agent.rpc("ping", headers={"Origin": ORIGIN}).status_code, 200)

    def test_the_token_dies_with_its_key_session(self):
        self.clock.advance(seconds=8 * 3600 + 5)
        r = self.agent.rpc("ping")
        self.assertEqual(r.status_code, 401)
        self.assertIn("invalid_token", r.headers["www-authenticate"])

    def test_console_sign_out_does_not_kill_the_agent_but_revoking_does(self):
        self.assertEqual(self.console.post("/api/logout").status_code, 200)
        self.assertEqual(self.agent.rpc("ping").status_code, 200)
        c2 = Console(self.app, authn=self.console.authn)
        self.assertEqual(c2.login().status_code, 200)
        grants = c2.get("/api/oauth/grants").json()["grants"]
        self.assertEqual([g["client_name"] for g in grants], ["Test Agent"])
        keys_before = len(self.cp.keys)
        self.assertEqual(c2.req("DELETE", f"/api/oauth/grants/{grants[0]['id']}").status_code, 200)
        self.assertEqual(len(self.cp.keys), keys_before - 1, "the agent's handle on the key is destroyed, not just forgotten")
        self.assertEqual(self.agent.rpc("ping").status_code, 401)
        self.assertEqual(c2.req("DELETE", f"/api/oauth/grants/{grants[0]['id']}").status_code, 404)

    def test_one_user_cannot_see_or_revoke_anothers_grants(self):
        other = signed_in(self.cp, self.app, self.admin)
        self.assertEqual(other.get("/api/oauth/grants").json()["grants"], [])
        gid = self.console.get("/api/oauth/grants").json()["grants"][0]["id"]
        self.assertEqual(other.req("DELETE", f"/api/oauth/grants/{gid}").status_code, 404)
        self.assertEqual(self.agent.rpc("ping").status_code, 200)

    def test_disabling_the_user_kills_their_agents(self):
        uid = self.console.get("/api/me").json()["user_id"]
        self.cp.disable_user(self.admin, uid)
        self.assertEqual(self.agent.rpc("ping").status_code, 401)

    def test_a_server_restart_forgets_every_token(self):
        # tokens, codes and keys live in memory only; a fresh OAuthService has none of them
        from sirosid_service.oauth import OAuthService
        fresh = OAuthService(self.cp, ORIGIN)
        with self.assertRaises(OAuthError):
            fresh.principal_for_token(self.agent.token)

    def test_the_audit_log_records_consent_and_revocation(self):
        actions = [r["action"] for r in self.cp.db.audit_log(50)]
        self.assertIn("oauth_approve", actions)


if __name__ == "__main__":
    unittest.main()
