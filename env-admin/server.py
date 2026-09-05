#!/usr/bin/env python3
"""env-admin: the one privileged, in-environment actor behind the dashboard's
"Clear all data" button (and `make storage-clear`, and the boot manager).

Runs inside the environment on both targets - a compose service locally, a
`sirosid-<env>-env-admin` Fly app - and is reached the same way on both: the
dashboard's nginx proxies /_admin/ to it, same-origin, exactly like
/_conformance/ reaches conformance-runner.

What a reset does, in order (streamed as SSE events so the dashboard and the
TUI can show progress):

  1. stop every Mongo consumer (wallet-backend, the vc services, the
     conformance suite) - nothing may write while the databases go
  2. drop the application databases; never admin/local/config, so Mongo's
     own root user survives on Fly
  3. start the consumers again and wait for them to be healthy - they
     recreate indexes and seed data at startup (wallet-backend's default
     tenant, vc-apigw's bootstrap documents), which is why a wipe without a
     restart leaves a stack that looks healthy but cannot issue
  4. re-register this environment's issuer and verifier with wallet-backend
     (scripts/bootstrap.py, the same code `make up` and `fly-up` run)

Stopping and starting is the only platform-specific part: the Docker Engine
API over the mounted socket locally, the Fly Machines API with per-app deploy
tokens on Fly. Everything else is plain pymongo and HTTP.

Wallet-backend without Mongo (the local stack's default, in-memory mode) is
handled by the same sequence: there is no database to drop, the restart
alone empties it.

Auth: POST /api/storage/reset needs `Authorization: Bearer <admin token>` -
the environment's existing wallet-backend admin token, the credential fly-up
already prints - and a body confirming the environment's name. GET endpoints
are unauthenticated (they expose sizes and counts, nothing else), matching
the dashboard's health proxies.

Configuration is by environment variable; see Config below. Nothing here is
sirosid-specific beyond the defaults, and the defaults describe the local
compose stack so `make up` needs no per-mode wiring: consumers that are not
running are simply skipped.
"""
import hmac
import http.client
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bootstrap  # noqa: E402  (scripts/bootstrap.py, copied next to this file by the Dockerfile)

VERSION = "0.1.0"
SYSTEM_DATABASES = {"admin", "local", "config"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env(name, default=""):
    return os.environ.get(name, default)


def _read_token():
    path = _env("ENV_ADMIN_TOKEN_FILE")
    if path and os.path.exists(path):
        return open(path).read().strip()
    return _env("ENV_ADMIN_TOKEN").strip()


# The local compose stack's Mongo consumers. `target` is the container name
# (docker) or Fly app (fly); `health` is an HTTP URL to poll after a start,
# for containers without a docker healthcheck (wallet-backend's image is
# distroless). Anything not currently present is skipped, so this one list
# serves every `make up` mode.
DEFAULT_DOCKER_CONSUMERS = [
    {"name": "wallet-backend", "target": "wallet-backend-e2e-test", "health": "http://wallet-backend:8080/health"},
    {"name": "vc-registry", "target": "vc-registry-e2e"},
    {"name": "vc-issuer", "target": "vc-issuer-e2e"},
    {"name": "vc-verifier", "target": "vc-verifier-e2e"},
    {"name": "vc-apigw", "target": "vc-apigw-e2e"},
    {"name": "conformance-server", "target": "conformance-suite-server"},
]
# "*" = every database except Mongo's own (admin/local/config). The vc
# services create databases per service and per cache (vc, vc_registry,
# verifier, verifier_cache, issuer-*_cache, ...) and the set has grown over
# time, so a fixed list would silently leave some behind. This Mongo serves
# nothing but this environment, and every consumer is restarted anyway.
DEFAULT_DATABASES = ["*"]


@dataclass
class Config:
    platform: str = _env("ENV_ADMIN_PLATFORM", "docker")          # docker | fly
    env_name: str = _env("ENV_ADMIN_ENV_NAME", "local")
    token: str = field(default_factory=_read_token)
    port: int = int(_env("PORT", "3002"))
    mongo_uri: str = _env("MONGO_URI", "mongodb://mongodb:27017")
    mongo_uri_file: str = _env("MONGO_URI_FILE", "")
    # Databases to drop: an explicit list, or "*" for every non-system one.
    databases: list = field(default_factory=lambda: [
        d.strip() for d in _env("MONGO_DATABASES", ",".join(DEFAULT_DATABASES)).split(",") if d.strip()])
    consumers: list = field(default_factory=lambda: json.loads(_env("CONSUMERS") or "null") or DEFAULT_DOCKER_CONSUMERS)
    docker_socket: str = _env("DOCKER_SOCKET", "/var/run/docker.sock")
    fly_api_url: str = _env("FLY_API_URL", "https://api.machines.dev")
    # {app: token} - one app-scoped deploy token per consumer, never an org token.
    fly_tokens: dict = field(default_factory=lambda: json.loads(_env("FLY_API_TOKENS") or "{}"))
    fly_tokens_file: str = _env("FLY_API_TOKENS_FILE", "")
    # Bootstrap after the restart. Any of these empty => step skipped.
    admin_url: str = _env("ADMIN_URL", "")
    issuer_url: str = _env("ISSUER_URL", "")
    verifier_url: str = _env("VERIFIER_URL", "")

    def resolve_files(self):
        if self.mongo_uri_file and os.path.exists(self.mongo_uri_file):
            self.mongo_uri = open(self.mongo_uri_file).read().strip()
        if self.fly_tokens_file and os.path.exists(self.fly_tokens_file):
            self.fly_tokens = json.loads(open(self.fly_tokens_file).read())
        return self


# ---------------------------------------------------------------------------
# Platform adapters: stop / start / state / health
# ---------------------------------------------------------------------------

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


class DockerPlatform:
    """Docker Engine API over the mounted socket - the standard dev-tooling
    pattern (what portainer and friends do). Only ever addresses containers
    by the exact names in the consumer list."""
    name = "docker"

    def __init__(self, cfg: Config):
        self.socket_path = cfg.docker_socket

    def _call(self, method, path, timeout=60):
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status >= 400 and resp.status != 304:
            raise RuntimeError(f"docker {method} {path}: HTTP {resp.status} {body[:200]!r}")
        return json.loads(body) if body and resp.getheader("Content-Type", "").startswith("application/json") else None

    def available(self) -> bool:
        return os.path.exists(self.socket_path)

    def inspect(self, target):
        try:
            return self._call("GET", f"/containers/{target}/json")
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def state(self, target) -> dict:
        info = self.inspect(target)
        if not info:
            return {"exists": False, "running": False, "health": None}
        st = info.get("State", {})
        return {"exists": True, "running": bool(st.get("Running")),
                "health": (st.get("Health") or {}).get("Status")}

    def stop(self, target):
        self._call("POST", f"/containers/{target}/stop?t=25", timeout=60)

    def start(self, target):
        self._call("POST", f"/containers/{target}/start")

    def has_healthcheck(self, target) -> bool:
        info = self.inspect(target) or {}
        return bool((info.get("Config") or {}).get("Healthcheck", {}).get("Test"))


class FlyPlatform:
    """Fly Machines API. One app-scoped deploy token per consumer app
    (fly-up creates them with `flyctl tokens create deploy -a <app>`), so a
    leaked env-admin can restart this environment's machines and nothing
    else."""
    name = "fly"

    def __init__(self, cfg: Config):
        self.base = cfg.fly_api_url.rstrip("/")
        self.tokens = cfg.fly_tokens

    def available(self) -> bool:
        return bool(self.tokens)

    def _call(self, method, app, path, timeout=60):
        token = self.tokens.get(app)
        if not token:
            raise RuntimeError(f"no Fly API token for app {app}")
        req = urllib.request.Request(f"{self.base}/v1/apps/{app}{path}", method=method,
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"fly {method} {app}{path}: HTTP {e.code} {e.read()[:200]!r}")

    def machines(self, app):
        return self._call("GET", app, "/machines") or []

    def state(self, target) -> dict:
        try:
            ms = self.machines(target)
        except RuntimeError:
            return {"exists": False, "running": False, "health": None}
        if not ms:
            return {"exists": False, "running": False, "health": None}
        m = ms[0]
        checks = m.get("checks") or []
        health = None
        if checks:
            health = "healthy" if all(c.get("status") == "passing" for c in checks) else "unhealthy"
        return {"exists": True, "running": m.get("state") == "started", "health": health}

    def stop(self, target):
        for m in self.machines(target):
            if m.get("state") not in ("stopped", "stopping"):
                self._call("POST", target, f"/machines/{m['id']}/stop")
        self._wait_state(target, "stopped")

    def start(self, target):
        for m in self.machines(target):
            if m.get("state") != "started":
                self._call("POST", target, f"/machines/{m['id']}/start")
        self._wait_state(target, "started")

    def _wait_state(self, target, state, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(m.get("state") == state for m in self.machines(target)):
                return
            time.sleep(2)
        raise RuntimeError(f"{target}: machines did not reach '{state}' within {timeout}s")

    def has_healthcheck(self, target) -> bool:
        ms = self.machines(target)
        return bool(ms and ms[0].get("checks"))


def make_platform(cfg: Config):
    return FlyPlatform(cfg) if cfg.platform == "fly" else DockerPlatform(cfg)


# ---------------------------------------------------------------------------
# Mongo
# ---------------------------------------------------------------------------

def mongo_client(cfg: Config):
    import pymongo  # imported lazily so /health works even if pymongo is missing
    return pymongo.MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)


def target_databases(cfg: Config, existing) -> list:
    """The databases a reset touches: the configured list, or with "*" every
    existing one that is not Mongo's own."""
    if "*" in cfg.databases:
        return sorted(d for d in existing if d not in SYSTEM_DATABASES)
    return [d for d in cfg.databases if d not in SYSTEM_DATABASES]


def mongo_stores(cfg: Config) -> dict:
    """Per-database size and document counts; reachable=False if Mongo is not there."""
    try:
        client = mongo_client(cfg)
        existing = set(client.list_database_names())
    except Exception as e:  # noqa: BLE001 - reported to the caller, not raised
        return {"reachable": False, "error": str(e).split("\n")[0][:200], "databases": []}
    dbs = []
    for name in target_databases(cfg, existing):
        if name not in existing:
            dbs.append({"name": name, "exists": False, "size_bytes": 0, "collections": 0, "documents": 0})
            continue
        try:
            st = client[name].command("dbStats")
            dbs.append({"name": name, "exists": True,
                        "size_bytes": int(st.get("dataSize", 0)) + int(st.get("indexSize", 0)),
                        "collections": int(st.get("collections", 0)), "documents": int(st.get("objects", 0))})
        except Exception as e:  # noqa: BLE001
            dbs.append({"name": name, "exists": True, "error": str(e)[:200]})
    client.close()
    return {"reachable": True, "databases": dbs}


def drop_databases(cfg: Config, log) -> list:
    client = mongo_client(cfg)
    existing = set(client.list_database_names())
    dropped = []
    for name in target_databases(cfg, existing):
        if name not in existing:
            continue
        client.drop_database(name)
        dropped.append(name)
        log(f"dropped database {name}")
    client.close()
    return dropped


# ---------------------------------------------------------------------------
# Reset job
# ---------------------------------------------------------------------------

class Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.queues = set()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.queues.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.queues.discard(q)

    def emit(self, event, data):
        with self.lock:
            for q in list(self.queues):
                q.put((event, data))


class ResetJob(threading.Thread):
    def __init__(self, cfg: Config, platform, broadcaster: Broadcaster, job_id: str, history: list):
        super().__init__(daemon=True)
        self.cfg, self.platform, self.bc, self.id = cfg, platform, broadcaster, job_id
        self.record = {"id": job_id, "status": "running", "started_at": time.time(), "steps": [],
                       "dropped": [], "restarted": [], "error": None}
        history.append(self.record)

    def log(self, message, level="info"):
        entry = {"t": time.time(), "level": level, "message": message}
        self.record["steps"].append(entry)
        self.bc.emit("reset_step", {"id": self.id, **entry})
        print(f"[reset {self.id}] {message}", flush=True)

    def run(self):
        try:
            self._run()
            self.record["status"] = "finished"
        except Exception as e:  # noqa: BLE001
            self.record["status"] = "error"
            self.record["error"] = str(e)
            self.log(f"reset failed: {e}", "error")
            traceback.print_exc()
        finally:
            self.record["finished_at"] = time.time()
            self.bc.emit("reset_finished", {"id": self.id, "status": self.record["status"],
                                            "error": self.record["error"]})

    def _present_consumers(self):
        present = []
        for c in self.cfg.consumers:
            st = self.platform.state(c["target"])
            if st["exists"]:
                present.append((c, st))
            else:
                self.log(f"{c['name']}: not part of this stack, skipping")
        return present

    def _run(self):
        self.bc.emit("reset_start", {"id": self.id})
        if not self.platform.available():
            raise RuntimeError(f"{self.platform.name} control is not available to env-admin "
                               "(no docker socket / no Fly tokens) - cannot restart consumers safely")
        present = self._present_consumers()

        self.log("step 1/4: stopping consumers")
        for c, st in present:
            if st["running"]:
                self.platform.stop(c["target"])
                self.log(f"stopped {c['name']}")

        self.log("step 2/4: dropping databases")
        stores = mongo_stores(self.cfg)
        if stores["reachable"]:
            self.record["dropped"] = drop_databases(self.cfg, self.log)
            if not self.record["dropped"]:
                self.log("no application databases existed - nothing to drop")
        else:
            self.log(f"Mongo not reachable ({stores.get('error', 'unknown')}) - only in-memory stores to clear")

        self.log("step 3/4: starting consumers")
        for c, _st in present:
            self.platform.start(c["target"])
            self.record["restarted"].append(c["name"])
            self.log(f"started {c['name']}")
        for c, _st in present:
            self._wait_healthy(c)

        self.log("step 4/4: re-registering issuer and verifier")
        names = {c["name"] for c, _ in present}
        if not (self.cfg.admin_url and self.cfg.issuer_url and self.cfg.verifier_url):
            self.log("bootstrap not configured (ADMIN_URL/ISSUER_URL/VERIFIER_URL) - skipped")
        elif "vc-apigw" not in names:
            self.log("VC services not running in this stack - nothing to register")
        else:
            bootstrap.register(self.cfg.admin_url, self.cfg.token, self.cfg.issuer_url, self.cfg.verifier_url,
                               log=self.log)
        self.log("done - the environment is back to its freshly deployed state")

    def _wait_healthy(self, consumer, timeout=120):
        target = consumer["target"]
        deadline = time.monotonic() + timeout
        url = consumer.get("health")
        uses_platform_health = self.platform.has_healthcheck(target)
        while time.monotonic() < deadline:
            if url:
                try:
                    with urllib.request.urlopen(url, timeout=3) as r:
                        if 200 <= r.status < 300:
                            self.log(f"{consumer['name']}: healthy")
                            return
                except Exception:  # noqa: BLE001
                    pass
            elif uses_platform_health:
                if self.platform.state(target).get("health") == "healthy":
                    self.log(f"{consumer['name']}: healthy")
                    return
            else:
                if self.platform.state(target).get("running"):
                    time.sleep(3)
                    self.log(f"{consumer['name']}: running (no health check to wait for)")
                    return
            time.sleep(2)
        self.log(f"{consumer['name']}: not healthy after {timeout}s - continuing", "warn")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.platform = make_platform(cfg)
        self.bc = Broadcaster()
        self.history = []
        self.lock = threading.Lock()
        self.active = None

    def status(self) -> dict:
        consumers = []
        for c in self.cfg.consumers:
            st = self.platform.state(c["target"])
            if st["exists"]:
                consumers.append({"name": c["name"], "target": c["target"],
                                  "state": "running" if st["running"] else "stopped", "health": st["health"]})
        last = self.history[-1] if self.history else None
        return {
            "env": self.cfg.env_name, "platform": self.cfg.platform, "version": VERSION,
            "control_available": self.platform.available(),
            "mongo": mongo_stores(self.cfg),
            "consumers": consumers,
            "reset_in_progress": bool(self.active and self.active.is_alive()),
            "last_reset": {k: last[k] for k in ("id", "status", "started_at", "finished_at", "dropped", "error")
                           if k in last} if last else None,
            "bootstrap_configured": bool(self.cfg.admin_url and self.cfg.issuer_url and self.cfg.verifier_url),
        }

    def start_reset(self) -> str:
        with self.lock:
            if self.active and self.active.is_alive():
                raise ResetBusy(self.active.id)
            job_id = f"reset-{int(time.time())}"
            self.active = ResetJob(self.cfg, self.platform, self.bc, job_id, self.history)
            self.active.start()
            return job_id


class ResetBusy(Exception):
    pass


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"sirosid-env-admin/{VERSION}"

        def log_message(self, fmt, *args):  # quieter than the default
            if self.path not in ("/health",):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or not state.cfg.token:
                return False
            return hmac.compare_digest(header[len("Bearer "):].strip(), state.cfg.token)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/health":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/storage":
                self._json(200, state.status())
            elif path == "/api/resets":
                self._json(200, state.history[-20:])
            elif path == "/api/events":
                self._sse()
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if path != "/api/storage/reset":
                return self._json(404, {"error": "not found"})
            if not self._authorized():
                return self._json(401, {"error": "admin token required (Authorization: Bearer <adminToken>)"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "body must be JSON"})
            if body.get("confirm") != state.cfg.env_name:
                return self._json(400, {"error": f"body.confirm must equal the environment name ({state.cfg.env_name!r})"})
            try:
                job_id = state.start_reset()
            except ResetBusy as e:
                return self._json(409, {"error": f"a reset ({e}) is already in progress"})
            self._json(202, {"id": job_id})

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = state.bc.subscribe()
            try:
                self.wfile.write(b"event: connected\ndata: {}\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event, data = q.get(timeout=15)
                        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.bc.unsubscribe(q)

    return Handler


def main():
    cfg = Config().resolve_files()
    if not cfg.token:
        print("WARNING: no ENV_ADMIN_TOKEN set - every reset request will be rejected", file=sys.stderr)
    state = State(cfg)
    server = ThreadingHTTPServer(("", cfg.port), make_handler(state))
    server.daemon_threads = True
    print(f"env-admin {VERSION}: env={cfg.env_name} platform={cfg.platform} port={cfg.port} "
          f"mongo={cfg.mongo_uri.split('@')[-1]} databases={cfg.databases}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
