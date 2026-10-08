"""A stateful fake of the Fly Machines API and of registry.fly.io, for the
single-machine layout's tests (sirosid_core.machines / sirosid_core.oci).

Shares app state with a tests/fakefly.FakeFly: apps are created and destroyed
through flyctl there, and a secret set through the Machines API shows up in
`flyctl secrets list`, as on real Fly. Modelled from real responses (2026-10):
container states in GET machine `containers[]` (healthy / stopped after an
init container exits), the secrets endpoint echoing the value with a version,
`/wait` answering 408 when the state is not reached.

Also enforces two real limits the fake would otherwise hide: a config body over
~1 MiB is refused, and a machine with more block devices than Fly can attach
never boots (stays `created`).
"""
import json
import re
from urllib.parse import parse_qs, urlsplit


class FakeMachines:
    BODY_LIMIT = 1_000_000   # real Fly: 989,744 B accepted, 1,056,412 B refused

    def __init__(self, fakefly):
        self.fly = fakefly
        self.machines = {}       # app -> {id: machine}
        self.versions = {}       # app -> secrets version
        self.log = []            # "METHOD /path"
        self.auth_seen = []      # Authorization headers
        self.bodies = []         # (path, body bytes) of writes - to prove what was (not) sent
        self.blobs = {}          # registry: digest -> bytes
        self.manifests = {}      # registry: "repo:tag" -> bytes
        self.registry_auth = []
        self.exec_result = {"stdout": "dropped wallet-backend\ndropped vc\n", "stderr": "", "exit_code": 0}
        self.fail = {}           # "METHOD path-regex" -> (status, body) to inject failures
        self._n = 0

    # ---- Machines API -----------------------------------------------------------

    def transport(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        path = u.path[len("/v1"):] if u.path.startswith("/v1") else u.path
        self.log.append(f"{method} {path}")
        self.auth_seen.append(headers.get("Authorization"))
        if body is not None:
            self.bodies.append((path, body))
        for key, (status, out) in self.fail.items():
            m, rx = key.split(" ", 1)
            if m == method and re.search(rx, path):
                return status, json.dumps(out).encode()
        if body is not None and len(body) > self.BODY_LIMIT:
            return 400, b'{"error":"request body too large"}'
        data = json.loads(body) if body else None
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != "apps":
            return 404, b'{"error":"not found"}'
        app = parts[1]
        if app not in self.fly.apps:
            return 404, json.dumps({"error": f"app {app} not found"}).encode()
        ms = self.machines.setdefault(app, {})
        rest = parts[2:]
        if rest[:1] == ["secrets"]:
            if method == "POST" and len(rest) == 2:
                v = self.versions[app] = self.versions.get(app, 100) + 1
                self.fly.apps[app]["secrets"][rest[1]] = data["value"]
                return 201, json.dumps({"name": rest[1], "value": data["value"], "version": v}).encode()
            if method == "GET":
                return 200, json.dumps({"secrets": [{"name": n} for n in self.fly.apps[app]["secrets"]]}).encode()
        if rest == ["machines"] and method == "GET":
            # An apps-layout app's machines were made by `flyctl deploy` (fakefly): the
            # same machines, seen through the API, as on real Fly.
            return 200, json.dumps(list(ms.values()) or self.fly.apps[app]["machines"]).encode()
        if rest == ["machines"] and method == "POST":
            self._n += 1
            mid = f"m{self._n:04d}"
            ms[mid] = {"id": mid, "name": data.get("name", ""), "region": data.get("region", ""),
                       "config": data["config"], "instance_id": f"i{self._n}", "updates": 0}
            self._boot(ms[mid])
            return 200, json.dumps(ms[mid]).encode()
        if len(rest) >= 2 and rest[0] == "machines":
            m = ms.get(rest[1])
            if m is None:
                return 404, b'{"error":"machine not found"}'
            action = rest[2] if len(rest) > 2 else ""
            if method == "GET" and not action:
                return 200, json.dumps(m).encode()
            if method == "POST" and not action:
                m["config"] = data["config"]
                m["updates"] += 1
                self._n += 1
                m["instance_id"] = f"i{self._n}"
                self._boot(m)
                return 200, json.dumps(m).encode()
            if method == "DELETE" and not action:
                del ms[rest[1]]
                return 200, b"{}"
            if action == "wait":
                want = parse_qs(u.query).get("state", ["started"])[0]
                return (200, b'{"ok":true}') if m["state"] == want else (408, b'{"error":"deadline_exceeded"}')
            if action == "stop":
                m["state"] = "stopped"
                for c in m["containers"]:
                    c["state"] = "stopped"
                return 200, b"{}"
            if action in ("start", "restart"):
                self._boot(m)
                return 200, b"{}"
            if action in ("cordon", "uncordon"):
                # Real Fly: idempotent, {"ok": true}; survives start, stop and update.
                m["cordoned"] = action == "cordon"
                return 200, b'{"ok":true}'
            if action == "exec":
                m.setdefault("execs", []).append(data)
                return 200, json.dumps(self.exec_result).encode()
        return 404, json.dumps({"error": f"fake: unhandled {method} {path}"}).encode()

    def _boot(self, m):
        cfg = m["config"]
        images = {c["image"] for c in cfg.get("containers", [])}
        vols = cfg.get("volumes") or []
        drives = len(images) + sum(1 for v in vols if "image" in v) + len(cfg.get("mounts") or [])
        if drives > 12:
            m["state"], m["containers"] = "created", [{"name": c["name"], "state": "unknown"}
                                                      for c in cfg.get("containers", [])]
            return
        m["state"] = "started"
        m["containers"] = [{"name": c["name"],
                            "state": "stopped" if (c.get("restart") or {}).get("policy") == "no" else "healthy"}
                           for c in cfg.get("containers", [])]

    def only(self, app):
        ms = list(self.machines.get(app, {}).values())
        assert len(ms) == 1, ms
        return ms[0]

    # ---- registry.fly.io ----------------------------------------------------------

    def registry_transport(self, method, url, headers, body, timeout):
        u = urlsplit(url)
        if "Authorization" not in headers:
            return 401, {"WWW-Authenticate": 'Basic realm="fake-registry"'}, b""
        self.registry_auth.append(headers["Authorization"])
        m = re.match(r"^/v2/(.+)/(blobs|manifests)/(.*)$", u.path)
        if not m:
            return 404, {}, b""
        repo, kind, ref = m.groups()
        if kind == "blobs" and method == "HEAD":
            return (200 if ref in self.blobs else 404), {}, b""
        if kind == "blobs" and method == "POST" and ref == "uploads/":
            return 202, {"Location": f"/v2/{repo}/blobs/uploads/u{len(self.blobs)}"}, b""
        if kind == "blobs" and method == "PUT":
            digest = parse_qs(u.query)["digest"][0]
            self.blobs[digest] = body
            return 201, {}, b""
        if kind == "manifests" and method == "PUT":
            self.manifests[f"{repo}:{ref}"] = body
            return 201, {}, b""
        return 404, {}, b""
