#!/usr/bin/env python3
"""env-admin/server.py: the reset sequence and the HTTP contract, with the
platform and Mongo faked out - no Docker or Mongo needed.

    python3 -m unittest tests/test_env_admin.py
"""
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "env-admin"))
sys.path.insert(0, str(ROOT / "scripts"))
import server  # noqa: E402


class FakePlatform:
    name = "fake"

    def __init__(self, present):
        self.present = set(present)
        self.running = set(present)
        self.calls = []

    def available(self):
        return True

    def state(self, target):
        return {"exists": target in self.present, "running": target in self.running, "health": "healthy"}

    def stop(self, target):
        self.calls.append(("stop", target))
        self.running.discard(target)

    def start(self, target):
        self.calls.append(("start", target))
        self.running.add(target)

    def has_healthcheck(self, target):
        return True


class ResetSequence(unittest.TestCase):
    def setUp(self):
        self.cfg = server.Config(token="secret", env_name="unit", consumers=[
            {"name": "wallet-backend", "target": "wb"}, {"name": "vc-apigw", "target": "apigw"},
            {"name": "absent", "target": "nope"}], databases=["wallet-backend", "vc", "admin"])
        self.dropped = []
        self.registered = []
        server_mod = server
        self._orig = (server_mod.mongo_stores, server_mod.drop_databases, server_mod.bootstrap.register)
        server_mod.mongo_stores = lambda cfg: {"reachable": True, "databases": []}

        def fake_drop(cfg, log):
            self.dropped.extend(d for d in cfg.databases if d not in server.SYSTEM_DATABASES)
            return list(self.dropped)
        server_mod.drop_databases = fake_drop
        server_mod.bootstrap.register = lambda *a, **k: self.registered.append(a) or {}

    def tearDown(self):
        server.mongo_stores, server.drop_databases, server.bootstrap.register = self._orig

    def run_job(self, cfg, platform):
        history = []
        job = server.ResetJob(cfg, platform, server.Broadcaster(), "reset-1", history)
        job.start()
        job.join(timeout=10)
        return history[0], platform

    def test_stop_drop_start_bootstrap_order(self):
        platform = FakePlatform({"wb", "apigw"})
        record, platform = self.run_job(self.cfg, platform)
        self.assertEqual(record["status"], "finished", record)
        self.assertEqual(platform.calls, [("stop", "wb"), ("stop", "apigw"), ("start", "wb"), ("start", "apigw")])
        self.assertEqual(self.dropped, ["wallet-backend", "vc"])  # never the admin db
        self.assertEqual(record["restarted"], ["wallet-backend", "vc-apigw"])

    def test_bootstrap_skipped_without_vc(self):
        self.cfg.admin_url = self.cfg.issuer_url = self.cfg.verifier_url = "http://x"
        record, _ = self.run_job(self.cfg, FakePlatform({"wb"}))
        self.assertEqual(record["status"], "finished")
        self.assertEqual(self.registered, [])
        self.assertTrue(any("nothing to register" in s["message"] for s in record["steps"]))

    def test_bootstrap_runs_with_vc(self):
        self.cfg.admin_url = self.cfg.issuer_url = self.cfg.verifier_url = "http://x"
        record, _ = self.run_job(self.cfg, FakePlatform({"wb", "apigw"}))
        self.assertEqual(record["status"], "finished")
        self.assertEqual(len(self.registered), 1)

    def test_no_control_is_an_error_not_a_wipe(self):
        platform = FakePlatform({"wb"})
        platform.available = lambda: False
        record, _ = self.run_job(self.cfg, platform)
        self.assertEqual(record["status"], "error")
        self.assertEqual(self.dropped, [])


class HttpContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = server.Config(token="secret", env_name="unit", consumers=[], databases=[])
        cls.state = server.State(cfg)
        cls.state.platform = FakePlatform(set())
        server.mongo_stores = lambda cfg: {"reachable": False, "error": "faked", "databases": []}
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.state))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_status_is_public(self):
        code, body = self.call("GET", "/api/storage")
        self.assertEqual(code, 200)
        self.assertEqual(body["env"], "unit")
        self.assertFalse(body["mongo"]["reachable"])

    def test_reset_requires_token_and_confirmation(self):
        self.assertEqual(self.call("POST", "/api/storage/reset", {"confirm": "unit"})[0], 401)
        self.assertEqual(self.call("POST", "/api/storage/reset", {"confirm": "unit"}, token="wrong")[0], 401)
        self.assertEqual(self.call("POST", "/api/storage/reset", {"confirm": "other"}, token="secret")[0], 400)
        code, body = self.call("POST", "/api/storage/reset", {"confirm": "unit"}, token="secret")
        self.assertEqual(code, 202)
        self.assertTrue(body["id"].startswith("reset-"))
        time.sleep(0.5)
        code, body = self.call("GET", "/api/resets")
        self.assertEqual(code, 200)
        self.assertEqual(body[-1]["id"], (self.state.history[-1]["id"]))

    def test_unknown_path_404(self):
        self.assertEqual(self.call("GET", "/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
