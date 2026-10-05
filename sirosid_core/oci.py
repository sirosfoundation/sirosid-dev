"""A one-layer, FROM-scratch image of plain files, pushed to a registry without docker.

A single-machine instance ships its large shared files (credential metadata,
presentation requests, bootstrap documents, branding) as ONE per-instance
"config bundle" image, mounted read-only into the containers that need them: a
Machines API config body is capped at ~1 MiB, and the vctms alone are ~350 KB
going to three containers.

Building that image needs nothing docker does: it is one tar layer, a config
blob and a manifest. Doing it here keeps the registry credential in process
memory (`docker login` / `flyctl auth docker` would write it to
~/.docker/config.json) and works where there is no docker daemon (a hosted
service). Standard library only; `transport` is injectable for tests.

The layer is deterministic (sorted entries, fixed mtime/owner, gzip mtime 0), so
the same files give the same digest: an unchanged bundle is a no-op push and the
machine config that names it by digest does not change.
"""
import base64
import gzip
import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.parse
import urllib.request

MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
CONFIG_V1 = "application/vnd.docker.container.image.v1+json"
LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"


class RegistryError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def build_layer(files: dict) -> tuple:
    """files: {relative posix path: bytes}. Returns (tar bytes, gzipped bytes)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        dirs = set()
        for path in sorted(files):
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"bundle path {path!r} must be relative and stay inside the image")
            parts = path.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, 0
                    tf.addfile(ti)
            data = files[path]
            ti = tarfile.TarInfo(path)
            ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
            tf.addfile(ti, io.BytesIO(data))
    tar = raw.getvalue()
    return tar, gzip.compress(tar, compresslevel=6, mtime=0)


def image_blobs(files: dict) -> dict:
    """Everything a push sends, by digest, plus the manifest. Pure."""
    tar, gz = build_layer(files)
    layer_digest = "sha256:" + hashlib.sha256(gz).hexdigest()
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(tar).hexdigest()]}},
                        sort_keys=True, separators=(",", ":")).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps({
        "schemaVersion": 2, "mediaType": MANIFEST_V2,
        "config": {"mediaType": CONFIG_V1, "size": len(config), "digest": config_digest},
        "layers": [{"mediaType": LAYER_GZIP, "size": len(gz), "digest": layer_digest}],
    }, sort_keys=True, separators=(",", ":")).encode()
    return {"blobs": {layer_digest: gz, config_digest: config}, "manifest": manifest,
            "digest": "sha256:" + hashlib.sha256(manifest).hexdigest()}


class Registry:
    """A Docker Registry v2 client that can push. Handles Basic auth (what
    registry.fly.io asks for) and the Bearer token dance (most other registries)."""

    def __init__(self, host: str, username: str, password: str, transport=None, timeout: float = 120):
        self.host = host
        self._basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._transport = transport or registry_transport
        self.timeout = timeout
        self._auth = None   # the Authorization header value once a challenge was answered

    def __repr__(self):
        return f"Registry(host={self.host!r})"

    def _req(self, method, url, body=None, headers=None, scope=""):
        for attempt in (0, 1):
            h = dict(headers or {})
            if self._auth:
                h["Authorization"] = self._auth
            status, rheaders, raw = self._send(method, url, h, body)
            if status == 401 and attempt == 0:
                self._answer(rheaders.get("www-authenticate", ""), scope)
                continue
            return status, rheaders, raw
        return status, rheaders, raw

    def _send(self, method, url, headers, body):
        result = self._transport(method, url, headers, body, self.timeout)
        # The machines transport returns (status, body); registries need headers
        # (Location, WWW-Authenticate), so a registry transport returns three.
        if len(result) == 2:
            raise RegistryError("the registry transport must return (status, headers, body)")
        status, rheaders, raw = result
        return status, {k.lower(): v for k, v in (rheaders or {}).items()}, raw

    def _answer(self, challenge: str, scope: str):
        kind, _, rest = challenge.partition(" ")
        if kind.lower() == "basic" or not challenge:
            self._auth = "Basic " + self._basic
            return
        params = {}
        for part in rest.split(","):
            k, _, v = part.strip().partition("=")
            params[k.strip()] = v.strip().strip('"')
        q = {"service": params.get("service", "")}
        if scope:
            q["scope"] = scope
        status, _, raw = self._send("GET", params["realm"] + "?" + urllib.parse.urlencode(q),
                                    {"Authorization": "Basic " + self._basic}, None)
        if status != 200:
            raise RegistryError(f"registry token request refused (HTTP {status})", status)
        body = json.loads(raw or b"{}")
        self._auth = "Bearer " + (body.get("token") or body.get("access_token") or "")

    def push(self, repo: str, tag: str, files: dict) -> str:
        """Push the files as an image; returns "<host>/<repo>@<digest>"."""
        img = image_blobs(files)
        scope = f"repository:{repo}:pull,push"
        base = f"https://{self.host}/v2/{repo}"
        for digest, blob in img["blobs"].items():
            status, _, _ = self._req("HEAD", f"{base}/blobs/{digest}", scope=scope)
            if status == 200:
                continue
            status, h, raw = self._req("POST", f"{base}/blobs/uploads/", body=b"", scope=scope)
            if status != 202 or not h.get("location"):
                raise RegistryError(f"starting a blob upload to {self.host}/{repo} failed (HTTP {status})", status)
            loc = h["location"]
            if loc.startswith("/"):
                loc = f"https://{self.host}{loc}"
            sep = "&" if "?" in loc else "?"
            status, _, _ = self._req("PUT", f"{loc}{sep}digest={urllib.parse.quote(digest)}", body=blob,
                                     headers={"Content-Type": "application/octet-stream"}, scope=scope)
            if status != 201:
                raise RegistryError(f"uploading {digest[:19]} to {self.host}/{repo} failed (HTTP {status})", status)
        status, _, _ = self._req("PUT", f"{base}/manifests/{tag}", body=img["manifest"],
                                 headers={"Content-Type": MANIFEST_V2}, scope=scope)
        if status not in (200, 201):
            raise RegistryError(f"pushing the manifest to {self.host}/{repo}:{tag} failed (HTTP {status})", status)
        return f"{self.host}/{repo}@{img['digest']}"


def registry_transport(method, url, headers, body, timeout):
    """urllib transport that also returns the response headers."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
