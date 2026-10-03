"""SQLite storage for the control plane.

One file, one writer lock: this service is invite-only and small, and SQLite on a
volume with continuous backup is plenty. Everything is plain rows and JSON; no ORM.
Times are epoch seconds. `seal`/`unseal` wrap instance state at rest - identity by
default, the hook where envelope encryption goes before real secrets are stored.
"""
import hashlib
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, role TEXT NOT NULL DEFAULT 'member',
  capabilities TEXT NOT NULL DEFAULT '[]', max_concurrent INTEGER NOT NULL DEFAULT 2,
  max_kept INTEGER NOT NULL DEFAULT 0, kept_until REAL, disabled INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  token_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
  capabilities TEXT NOT NULL DEFAULT '[]', max_concurrent INTEGER NOT NULL DEFAULT 2,
  max_kept INTEGER NOT NULL DEFAULT 0, kept_for_days REAL, email TEXT, expires_at REAL NOT NULL,
  used_by TEXT, used_at REAL, revoked INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS configs(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, doc TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(owner, name));
CREATE TABLE IF NOT EXISTS instances(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  spec TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
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
    def __init__(self, path=":memory:", clock=time.time, seal=None, unseal=None):
        self.clock = clock
        self.seal = seal or (lambda b: b)
        self.unseal = unseal or (lambda b: b)
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


class DbStateStore:
    """sirosid_core.state.StateStore backed by the `state` table."""

    def __init__(self, db: Database):
        self.db = db

    def load(self, instance_id):
        rows = self.db.all("SELECT path, data FROM state WHERE instance_id=?", (instance_id,))
        return {r["path"]: self.db.unseal(bytes(r["data"])) for r in rows}

    def save(self, instance_id, blobs):
        with self.db.transaction() as d:
            d.execute("DELETE FROM state WHERE instance_id=?", (instance_id,))
            for path, data in blobs.items():
                d.execute("INSERT INTO state(instance_id, path, data) VALUES(?,?,?)",
                          (instance_id, path, self.db.seal(bytes(data))))

    def delete(self, instance_id):
        self.db.execute("DELETE FROM state WHERE instance_id=?", (instance_id,))
