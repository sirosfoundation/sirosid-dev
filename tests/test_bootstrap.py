#!/usr/bin/env python3
"""scripts/bootstrap.py: the HTTP contract with wallet-backend's admin API,
with urllib faked out - no wallet-backend needed.

    python3 -m unittest tests/test_bootstrap.py
"""
import contextlib
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap  # noqa: E402

ADMIN = "http://wb:8081"
TENANT = f"{ADMIN}/admin/tenants/default"
ISSUER = "http://issuer.example"
VERIFIER = "http://verifier.example"


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeAdminAPI:
    """Scripted responses keyed by (method, url); records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        status, payload = self.routes.get((req.get_method(), req.full_url), (404, None))
        if status >= 400:
            raw = json.dumps(payload).encode() if payload is not None else b""
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(raw))
        return FakeResponse(status, payload)


class BootstrapContract(unittest.TestCase):
    def setUp(self):
        self._orig = (bootstrap.urllib.request.urlopen, bootstrap.time.sleep)
        bootstrap.time.sleep = lambda _s: None
        self.log = []

    def tearDown(self):
        bootstrap.urllib.request.urlopen, bootstrap.time.sleep = self._orig

    def install(self, routes):
        api = FakeAdminAPI(routes)
        bootstrap.urllib.request.urlopen = api
        return api

    def fresh_routes(self, existing_issuers=None):
        return {
            ("GET", TENANT): (200, {"id": "default"}),
            ("GET", f"{TENANT}/issuers"): (200, {"issuers": existing_issuers or []}),
            ("POST", f"{TENANT}/issuers"): (201, {}),
            ("POST", f"{TENANT}/verifiers"): (201, {}),
        }

    def test_every_call_carries_the_bearer_token(self):
        api = self.install(self.fresh_routes())
        bootstrap.register(ADMIN, "s3cret", ISSUER, VERIFIER, log=self.log.append)
        self.assertTrue(api.requests)
        for _method, url, headers, _body in api.requests:
            self.assertEqual(headers.get("Authorization"), "Bearer s3cret", url)
        posts = [(url, headers, body) for m, url, headers, body in api.requests if m == "POST"]
        for _url, headers, _body in posts:
            self.assertEqual(headers.get("Content-type"), "application/json")

    def test_fresh_environment_registers_issuer_and_verifier(self):
        api = self.install(self.fresh_routes())
        summary = bootstrap.register(ADMIN, "t", ISSUER, VERIFIER, log=self.log.append)
        self.assertEqual(summary, {"removed_stale_issuers": [], "issuer": "registered",
                                   "verifier": "registered"})
        issuer_post = next(b for m, u, _h, b in api.requests if m == "POST" and u == f"{TENANT}/issuers")
        self.assertEqual(issuer_post, {"credential_issuer_identifier": ISSUER, "visible": True,
                                       "client_id": bootstrap.ISSUER_CLIENT_ID})
        verifier_post = next(b for m, u, _h, b in api.requests if m == "POST" and u == f"{TENANT}/verifiers")
        self.assertEqual(verifier_post, {"name": "VC Verifier", "url": VERIFIER})

    def test_409_means_already_registered_and_is_not_an_error(self):
        routes = self.fresh_routes()
        routes[("POST", f"{TENANT}/issuers")] = (409, {"error": "exists"})
        routes[("POST", f"{TENANT}/verifiers")] = (409, {"error": "exists"})
        self.install(routes)
        summary = bootstrap.register(ADMIN, "t", ISSUER, VERIFIER, log=self.log.append)
        self.assertEqual(summary["issuer"], "already registered")
        self.assertEqual(summary["verifier"], "already registered")

    def test_stale_issuers_are_pruned_and_the_current_one_kept(self):
        existing = [
            {"id": "old-1", "credential_issuer_identifier": "http://old.example"},
            {"id": "keep", "credential_issuer_identifier": ISSUER},
            {"id": "old-2", "credential_issuer_identifier": "https://tunnel.trycloudflare.com"},
        ]
        routes = self.fresh_routes(existing)
        routes[("DELETE", f"{TENANT}/issuers/old-1")] = (204, None)
        routes[("DELETE", f"{TENANT}/issuers/old-2")] = (204, None)
        routes[("POST", f"{TENANT}/issuers")] = (409, None)
        api = self.install(routes)
        summary = bootstrap.register(ADMIN, "t", ISSUER, VERIFIER, log=self.log.append)
        deletes = sorted(u for m, u, _h, _b in api.requests if m == "DELETE")
        self.assertEqual(deletes, [f"{TENANT}/issuers/old-1", f"{TENANT}/issuers/old-2"])
        self.assertEqual(summary["removed_stale_issuers"],
                         ["http://old.example", "https://tunnel.trycloudflare.com"])
        self.assertEqual(summary["issuer"], "already registered")

    def test_rejected_token_fails_fast_instead_of_retrying(self):
        api = self.install({("GET", TENANT): (401, {"error": "unauthorized"})})
        with self.assertRaises(bootstrap.BootstrapError) as ctx:
            bootstrap.register(ADMIN, "wrong", ISSUER, VERIFIER, log=self.log.append)
        self.assertIn("rejected", str(ctx.exception))
        self.assertEqual(len(api.requests), 1)

    def test_tenant_not_ready_is_retried_then_gives_up(self):
        api = self.install({("GET", TENANT): (404, None)})
        with self.assertRaises(bootstrap.BootstrapError) as ctx:
            bootstrap.wait_for_tenant(ADMIN, "t", attempts=3, log=self.log.append)
        self.assertIn("never became ready", str(ctx.exception))
        self.assertEqual(len(api.requests), 3)

    def test_connection_errors_while_waiting_are_retried(self):
        calls = {"n": 0}
        good = FakeAdminAPI({("GET", TENANT): (200, {})})

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("connection refused")
            return good(req, timeout)

        bootstrap.urllib.request.urlopen = flaky
        bootstrap.wait_for_tenant(ADMIN, "t", attempts=5, log=self.log.append)
        self.assertEqual(calls["n"], 3)

    def test_hard_failure_registering_raises(self):
        routes = self.fresh_routes()
        routes[("POST", f"{TENANT}/issuers")] = (500, {"error": "boom"})
        self.install(routes)
        with self.assertRaises(bootstrap.BootstrapError) as ctx:
            bootstrap.register(ADMIN, "t", ISSUER, VERIFIER, log=self.log.append)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_main_maps_bootstrap_errors_to_exit_code_1(self):
        argv = ["--admin-url", ADMIN, "--admin-token", "x", "--issuer-url", ISSUER, "--verifier-url", VERIFIER]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.install({("GET", TENANT): (403, None)})
            self.assertEqual(bootstrap.main(argv), 1)
            self.install(self.fresh_routes())
            self.assertEqual(bootstrap.main(argv), 0)
        self.assertIn("admin token rejected", err.getvalue())
        self.assertIn("registered", out.getvalue())


if __name__ == "__main__":
    unittest.main()
