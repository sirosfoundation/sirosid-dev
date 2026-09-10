#!/usr/bin/env python3
"""scripts/storage.py: the volume-level fallback must never fire under a
running environment.

Regression: on 2026-09-10 `make fly-storage-clear ENV=gdc YES=yes` found
env-admin momentarily unreachable right after a redeploy and, with the prompt
skipped, destroyed gdc's Mongo machine and volume while every consumer was
still running. The reset path now retries env-admin and, if it still cannot
be reached, refuses when any Mongo consumer is running.

    python3 -m unittest tests/test_storage_clear_guard.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import storage  # noqa: E402


class FlyClearGuard(unittest.TestCase):
    def setUp(self):
        self.admin = storage.EnvAdmin("https://x.example/_admin", "tok")
        patches = [
            mock.patch.object(storage, "fly_env_admin", lambda env: self.admin),
            mock.patch.object(storage.EnvAdmin, "status", lambda self, attempts=1, timeout=5: None),
            mock.patch.object(storage, "flyctl", side_effect=AssertionError("flyctl must not be called")),
            mock.patch.object(storage, "fly_volumes", side_effect=AssertionError("volumes must not be listed")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_refuses_when_consumers_run(self):
        with mock.patch.object(storage, "fly_consumers_running", lambda env: ["sirosid-t-wallet-backend"]):
            with self.assertRaises(SystemExit) as cm:
                storage.fly_clear("t", yes=True)
        self.assertIn("Refusing to destroy the volume", str(cm.exception))

    def test_falls_through_only_when_nothing_runs(self):
        with mock.patch.object(storage, "fly_consumers_running", lambda env: []), \
             mock.patch.object(storage, "fly_volumes", lambda env: []):
            storage.fly_clear("t", yes=True)   # nothing to clear, no exception

    def test_status_retries_before_giving_up(self):
        calls = []

        def fake_req(self, method, path, body=None, timeout=10):
            calls.append(timeout)
            raise OSError("cold")
        with mock.patch.object(storage.EnvAdmin, "_req", fake_req), mock.patch.object(storage.time, "sleep", lambda s: None):
            self.assertIsNone(storage.EnvAdmin("http://x").status(attempts=3, timeout=7))
        self.assertEqual(calls, [7, 7, 7])


if __name__ == "__main__":
    unittest.main()
