"""OAuth + MCP end to end: a real ASGI app, a software passkey for the console half, and a bare
HTTP client (no cookies, no Origin) standing in for an MCP client such as Claude Code."""
import base64
import json
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


FINGERPRINT = ":".join(["AB"] * 32)
PROMPT_ARGS = {
    "spin-up-environment": {"template": "standard", "label": "demo"},
    "reconfigure-environment": {"instance": "aaaaaaaa", "change": "trust https://issuer.example.com"},
    "add-trusted-party": {"instance": "aaaaaaaa", "kind": "verifier", "url_or_identity": "x509_san_dns:verifier.example.com"},
    "test-android-passkeys": {"instance": "aaaaaaaa", "package": "org.example.app", "fingerprint": FINGERPRINT},
    "diagnose-environment": {"instance": "aaaaaaaa"},
    "clean-up-environments": {},
}


@NEEDS
class McpAgentSurfaceTests(unittest.TestCase):
    """What an agent gets beyond the tools: knowledge, templates and the schema as resources, the
    platform's skills as prompts, and the environment tools (config, reconfigure, health, activity)."""

    def setUp(self):
        from fakemachines import FakeMachines
        from sirosid_core.machines import MachinesClient
        self.cp, self.auth, self.app, self.admin, self.fake, self.clock = build()
        self.cp._machines = MachinesClient("tok", transport=FakeMachines(self.fake).transport, sleep=lambda s: None)
        self.console = signed_in(self.cp, self.app, self.admin)
        self.agent, _, _ = connect(self.app, self.console)

    def rpc(self, method, params=None):
        return self.agent.rpc(method, params).json()

    def tool_names(self):
        return {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}

    def test_initialize_advertises_tools_resources_and_prompts_and_says_read_first(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(set(r["capabilities"]), {"tools", "resources", "prompts"})
        for word in ("knowledge", "prompts", "get_instance_config", "reconfigure_instance", "BEFORE"):
            self.assertIn(word, r["instructions"])

    def test_the_new_tools_and_their_annotations(self):
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        for name in ("get_instance_config", "get_instance_health", "get_instance_activity", "get_knowledge", "list_example_prompts"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
            self.assertFalse(tools[name]["annotations"]["destructiveHint"], name)
        rc = tools["reconfigure_instance"]
        self.assertEqual((rc["annotations"]["readOnlyHint"], rc["annotations"]["destructiveHint"]), (False, False))
        self.assertIn("RESTARTS", rc["description"])
        self.assertEqual(rc["inputSchema"]["required"], ["id"])
        self.assertNotIn("focus_environment", tools, "UI-only tools are not MCP tools")

    def test_an_environment_through_the_new_tools(self):
        a = self.agent
        iid = a.call("create_instance", config={})["structuredContent"]["id"]
        self.assertEqual(a.call("get_instance_config", id=iid)["structuredContent"]["config"], {})
        a.call("save_config", name="partner", config={"trusted_issuers": ["https://i.example.com"]})
        r = a.call("reconfigure_instance", id=iid, config_name="partner")
        self.assertFalse(r["isError"], r["content"][0]["text"])
        self.assertEqual(r["structuredContent"]["config_name"], "partner")
        self.assertEqual(a.call("get_instance_config", id=iid)["structuredContent"]["config"]["trusted_issuers"], ["https://i.example.com"])
        bad = a.call("reconfigure_instance", id=iid, config={"mystery": 1})
        self.assertTrue(bad["isError"])
        self.assertIn("mystery", bad["content"][0]["text"])
        h = a.call("get_instance_health", id=iid)["structuredContent"]
        self.assertTrue(h["components"])
        acts = a.call("get_instance_activity", id=iid, limit=3)["structuredContent"]["activity"]
        self.assertEqual(len(acts), 3)
        self.assertEqual(a.rpc("tools/call", {"name": "get_instance_activity", "arguments": {"id": iid, "limit": "3"}}).json()["error"]["code"],
                         -32602, "an integer argument is type-checked")
        self.assertEqual(a.rpc("tools/call", {"name": "get_instance_activity", "arguments": {"id": iid, "limit": True}}).json()["error"]["code"],
                         -32602, "a boolean is not an integer")
        a.call("stop_instance", id=iid)
        stopped = a.call("reconfigure_instance", id=iid, config={})
        self.assertTrue(stopped["isError"])
        self.assertIn("start it first", stopped["content"][0]["text"])

    def test_get_knowledge(self):
        from sirosid_core import knowledge
        a = self.agent
        listing = a.call("get_knowledge")
        self.assertEqual([t["id"] for t in listing["structuredContent"]["topics"]], [t.id for t in knowledge.list_topics()])
        first = knowledge.list_topics()[0]
        one = a.call("get_knowledge", topic=first.id)
        self.assertFalse(one["isError"])
        self.assertTrue(one["content"][0]["text"].startswith(f"# {first.title}"), "full Markdown, not JSON")
        self.assertEqual(one["structuredContent"]["topic"]["body"], first.body)
        unknown = a.call("get_knowledge", topic="no-such-topic")
        self.assertTrue(unknown["isError"])
        self.assertIn(first.id, unknown["content"][0]["text"], "the error lists the valid ids")
        self.assertTrue(a.call("get_knowledge", topic="x", query="y")["isError"])
        hits = a.call("get_knowledge", query="environment")["structuredContent"]["hits"]
        self.assertTrue(hits and hits[0]["snippet"])
        self.assertEqual(a.call("get_knowledge", query="zzzzqqqq")["structuredContent"]["hits"], [])
        self.assertEqual(a.call("list_example_prompts")["structuredContent"]["examples"], knowledge.examples())

    def test_resources_list_and_read(self):
        from sirosid_core import knowledge
        res = {r["uri"]: r for r in self.rpc("resources/list")["result"]["resources"]}
        for t in knowledge.list_topics():
            self.assertEqual(res[f"sirosid://knowledge/{t.id}"]["mimeType"], "text/markdown")
        self.assertIn("sirosid://templates/standard", res)
        self.assertNotIn("sirosid://templates/custom-wallet-backend", res, "templates by capability, like list_config_templates")
        self.assertEqual(res["sirosid://schema/config"]["mimeType"], "application/json")
        for r in res.values():
            self.assertTrue(r["title"] and r["description"] and r["name"], r["uri"])
        for uri, r in res.items():
            got = self.rpc("resources/read", {"uri": uri})["result"]["contents"]
            self.assertEqual((got[0]["uri"], got[0]["mimeType"]), (uri, r["mimeType"]))
            self.assertTrue(got[0]["text"])
        schema = json.loads(self.rpc("resources/read", {"uri": "sirosid://schema/config"})["result"]["contents"][0]["text"])
        self.assertIn("trusted_issuers", schema["properties"])
        for uri in ("sirosid://knowledge/nope", "sirosid://knowledge/../../etc/passwd", "sirosid://templates/custom-wallet-backend",
                    "sirosid://schema/other", "file:///etc/passwd", "sirosid://", "nonsense"):
            err = self.rpc("resources/read", {"uri": uri})["error"]
            self.assertEqual(err["code"], -32002, uri)
        self.assertEqual(self.rpc("resources/read", {})["error"]["code"], -32602)

    def test_prompts_list_and_get(self):
        listed = {p["name"]: p for p in self.rpc("prompts/list")["result"]["prompts"]}
        self.assertEqual(set(listed), set(PROMPT_ARGS))
        self.assertEqual({a["name"]: a["required"] for a in listed["add-trusted-party"]["arguments"]},
                         {"instance": True, "kind": True, "url_or_identity": True})
        for name, args in PROMPT_ARGS.items():
            r = self.rpc("prompts/get", {"name": name, "arguments": args})["result"]
            self.assertEqual(r["messages"][0]["role"], "user", name)
            self.assertEqual(r["messages"][0]["content"]["type"], "text")
            self.assertIn("get_knowledge", r["messages"][0]["content"]["text"], "every runbook says what to read first")
        self.assertIn('"standard"', self.rpc("prompts/get", {"name": "spin-up-environment", "arguments": {"template": "standard"}})
                      ["result"]["messages"][0]["content"]["text"])
        self.assertTrue(self.rpc("prompts/get", {"name": "spin-up-environment"})["result"]["messages"], "optional arguments")

    def test_prompt_argument_errors_are_invalid_params(self):
        good = PROMPT_ARGS["test-android-passkeys"]
        cases = [{"name": "no-such-prompt"}, {"name": "diagnose-environment"},
                 {"name": "diagnose-environment", "arguments": {"instance": "aaaaaaaa", "extra": "x"}},
                 {"name": "diagnose-environment", "arguments": {"instance": "AAAA"}},
                 {"name": "diagnose-environment", "arguments": {"instance": 12345678}},
                 {"name": "diagnose-environment", "arguments": "aaaaaaaa"},
                 {"name": "add-trusted-party", "arguments": {**PROMPT_ARGS["add-trusted-party"], "kind": "wallet"}},
                 {"name": "test-android-passkeys", "arguments": {**good, "fingerprint": "not-a-fingerprint"}},
                 {"name": "test-android-passkeys", "arguments": {**good, "package": "noDots"}},
                 {"name": "reconfigure-environment", "arguments": {"instance": "aaaaaaaa", "change": "x" * 501}}]
        for params in cases:
            r = self.rpc("prompts/get", params)
            self.assertEqual(r.get("error", {}).get("code"), -32602, params)

    def test_prompts_name_only_tools_and_config_keys_that_exist(self):
        """Every backticked name in every runbook is a real tool or a real config key, so a renamed
        tool cannot leave an agent following instructions that no longer work."""
        import re
        from sirosid_core.policy import SAVED_CONFIG_KEYS
        tools = self.tool_names()
        allowed = tools | set(SAVED_CONFIG_KEYS)
        named = set()
        for name, args in PROMPT_ARGS.items():
            text = self.rpc("prompts/get", {"name": name, "arguments": args})["result"]["messages"][0]["content"]["text"]
            for ident in re.findall(r"`([a-z][a-z0-9_]*)", text):
                self.assertIn(ident, allowed, f"{name} names {ident!r}, which is neither a tool nor a config key")
                named.add(ident)
        self.assertGreaterEqual(len(named & tools), 12, "the runbooks really do name tools")
        for must in ("reconfigure_instance", "get_instance_health", "get_instance_config", "validate_config", "create_instance"):
            self.assertIn(must, named)

    def test_prompts_name_only_knowledge_topics_that_exist(self):
        import re
        import unittest.mock as mock
        from sirosid_core import knowledge
        topics = [knowledge.Topic("lifecycle", "Lifecycle", "s", "", 10, (), "body"),
                  knowledge.Topic("trust", "Trust", "s", "", 20, (), "body")]
        with mock.patch.object(knowledge, "list_topics", return_value=topics):
            text = self.rpc("prompts/get", {"name": "add-trusted-party", "arguments": PROMPT_ARGS["add-trusted-party"]}
                            )["result"]["messages"][0]["content"]["text"]
            self.assertIn('topic="trust"', text)
            all_named = set()
            for name, args in PROMPT_ARGS.items():
                t = self.rpc("prompts/get", {"name": name, "arguments": args})["result"]["messages"][0]["content"]["text"]
                all_named |= set(re.findall(r'topic="([a-z0-9-]+)"', t))
                self.assertIn('query="', t, "a search step always exists, whatever the knowledge base holds")
            self.assertTrue(all_named)
            self.assertLessEqual(all_named, {"lifecycle", "trust"}, "a topic that does not exist is never named")


if __name__ == "__main__":
    unittest.main()
