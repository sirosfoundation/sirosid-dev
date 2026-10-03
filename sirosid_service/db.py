"""SQLite storage for the control plane.

One file, one writer lock: this service is invite-only and small, and SQLite on a
volume with continuous backup is plenty. Everything is plain rows and JSON; no ORM.
Times are epoch seconds.

What is sealed (see vault.py) and what is not: a user's saved configs, an instance's
full spec and config, and its secrets (admin token, PKI, Mongo password) are sealed
under the owner's key; ids, owners, names, status, expiry, public URLs and the
`naming` (env, app prefix, hostname pattern) stay plaintext, because stop, start,
destroy, the reaper and the sweeper must work with nobody logged in. So a database
dump or a backup reveals who has which instances and when they expire, and nothing
the users put in them.
"""
import hashlib
import json
import sqlite3
import threading
import time

from .vault import aad

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, role TEXT NOT NULL DEFAULT 'member',
  capabilities TEXT NOT NULL DEFAULT '[]', max_concurrent INTEGER NOT NULL DEFAULT 2,
  max_kept INTEGER NOT NULL DEFAULT 0, kept_until REAL, disabled INTEGER NOT NULL DEFAULT 0,
  privatedata BLOB, key_check BLOB, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  token_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
  capabilities TEXT NOT NULL DEFAULT '[]', max_concurrent INTEGER NOT NULL DEFAULT 2,
  max_kept INTEGER NOT NULL DEFAULT 0, kept_for_days REAL, email TEXT, expires_at REAL NOT NULL,
  used_by TEXT, used_at REAL, revoked INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS configs(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, doc BLOB NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(owner, name));
CREATE TABLE IF NOT EXISTS instances(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  naming TEXT NOT NULL, spec BLOB NOT NULL, config BLOB NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  expires_at REAL, kept INTEGER NOT NULL DEFAULT 0, urls TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS instances_owner ON instances(owner);
CREATE TABLE IF NOT EXISTS state(
  instance_id TEXT NOT NULL, path TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(instance_id, path));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS orphans(app TEXT PRIMARY KEY, first_seen REAL NOT NULL);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path=":memory:", clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        with self._lock:
            self._c.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._c.close()

    def execute(self, sql, params=()):
        with self._lock:
            return self._c.execute(sql, params)

    def one(self, sql, params=()):
        r = self.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def transaction(self):
        return _Tx(self)

    def audit(self, actor, action, target="", **detail):
        self.execute("INSERT INTO audit(ts, actor, action, target, detail) VALUES(?,?,?,?,?)",
                     (self.clock(), actor, action, target, json.dumps(detail, sort_keys=True, default=str)))

    def audit_log(self, limit=100):
        return self.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, holding the writer lock: quota checks and the
    insert they guard must not interleave with another request's."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._c.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._c.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False


class SealedStateStore:
    """sirosid_core.state.StateStore for ONE instance, sealed under its owner's key.

    Built per operation from a live session's Sealer, so it cannot be used without
    one. A row copied to another instance or owner does not open (AAD).
    """

    def __init__(self, db: Database, sealer, owner_id: str):
        self.db = db
        self.sealer = sealer
        self.owner = owner_id

    def _aad(self, instance_id, path):
        return aad(self.owner, "state", f"{instance_id}/{path}")

    def load(self, instance_id):
        rows = self.db.all("SELECT path, data FROM state WHERE instance_id=?", (instance_id,))
        return {r["path"]: self.sealer.open(bytes(r["data"]), self._aad(instance_id, r["path"])) for r in rows}

    def save(self, instance_id, blobs):
        with self.db.transaction() as d:
            d.execute("DELETE FROM state WHERE instance_id=?", (instance_id,))
            for path, data in blobs.items():
                d.execute("INSERT INTO state(instance_id, path, data) VALUES(?,?,?)",
                          (instance_id, path, self.sealer.seal(bytes(data), self._aad(instance_id, path))))


def delete_state(db: Database, instance_id: str):
    """Removing state needs no key - which is what lets the reaper do it."""
    db.execute("DELETE FROM state WHERE instance_id=?", (instance_id,))
