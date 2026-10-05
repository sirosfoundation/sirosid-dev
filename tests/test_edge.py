"""The shared edge (sirosid_core/edge.py, edge/, scripts/edge-up.py).

What these hold: exactly the flat instance shape replays, to exactly `sid-<id>`,
and every adversarial Host (trailing dot, port, `..`, injections, `console`, the
edge's own name, over-long labels, non-public components) does not; the replay
header is never sent with `always`; the apex is static only; the domain is
validated strictly. The same corpus runs through REAL nginx (docker, or a local
nginx for `nginx -t`) when available, so the Python mirror cannot drift from what
nginx actually does.
"""
import http.client
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sirosid_core import edge  # noqa: E402
from sirosid_core.edge import (DEFAULT_COMPONENTS, EdgeConfigError, check_domain, check_edge_app,  # noqa: E402
                               edge_fly_toml, edge_nginx_conf, render_site, route)
from sirosid_core.singlemachine import PUBLIC_COMPONENTS  # noqa: E402

D = "sm.invalid"
ID = "k7m2qx4d"
NGINX_IMAGE = "nginx:1.29-alpine"

# (Host header, expected route, expected app)
CORPUS = [(f"{c}-{ID}.{D}", "replay", f"sid-{ID}") for c in DEFAULT_COMPONENTS] + [
    (f"vc-apigw-abcd1234.{D}", "replay", "sid-abcd1234"),
    (f"wallet-proxy-00000000.{D}", "replay", "sid-00000000"),
    (f"VC-APIGW-{ID.upper()}.SM.INVALID", "replay", f"sid-{ID}"),       # $host lower-cases
    (f"vc-apigw-{ID}.Sm.Invalid", "replay", f"sid-{ID}"),
    (f"vc-apigw-{ID}.{D}.", "404", None),                              # trailing dot
    (f"vc-apigw-{ID}.{D}:443", "404", None),                           # port suffix
    (f"vc-apigw-{ID}.{D}:8080", "404", None),
    (f"vc-apigw-{ID}..{D}", "400", None),                              # empty label
    (f"..{D}", "400", None),
    (f"vc-apigw-{ID}.{D};app=evil", "404", None),                      # injections
    (f"vc-apigw-{ID};app=evil.{D}", "404", None),
    (f"vc-apigw-{ID}.{D},app=evil", "404", None),
    (f"vc-apigw-{ID}.{D} app=evil", "400", None),
    (f"vc-apigw-{ID[:7]};.{D}", "404", None),
    (f"console.{D}", "404", None),                                     # the console is not the edge's
    (f"console-{ID}.{D}", "404", None),
    (f"console.{D}.evil.example", "404", None),
    ("sbx-edge-abc123.fly.dev", "404", None),                          # the edge's own name
    (f"sid-{ID}.{D}", "404", None),
    (f"sid-{ID}.fly.dev", "404", None),
    (f"vc-apigw-{ID}.{D}.evil.example", "404", None),                  # suffix / prefix games
    (f"x.vc-apigw-{ID}.{D}", "404", None),
    (f"xvc-apigw-{ID}.{D}", "404", None),
    (f"vc-apigw-{ID}.{D}x", "404", None),
    (f"vc-apigw-{ID}x{D}", "404", None),
    (f"vc-apigw--{ID}.{D}", "404", None),
    (f"vc-apigw-{ID[:7]}.{D}", "404", None),                           # id length
    (f"vc-apigw-{ID}9.{D}", "404", None),
    (f"vc-apigw-k7m2_x4d.{D}", "404", None),
    (f"vc-apigw-{'a' * 64}.{D}", "404", None),                         # 64+ char label
    ("a" * 64 + f".{D}", "404", None),
    (f"vc-issuer-{ID}.{D}", "404", None),                              # not public
    (f"wallet-backend-{ID}.{D}", "404", None),
    (f"pdp-{ID}.{D}", "404", None),
    (f"mongodb-{ID}.{D}", "404", None),
    (f"env-admin-{ID}.{D}", "404", None),
    (f"{ID}.{D}", "404", None),
    ("evil.example", "404", None),
    ("localhost", "404", None),
    ("127.0.0.1", "404", None),
    (D, "static", None),
    (f"www.{D}", "static", None),
    ("WWW.SM.INVALID", "static", None),
]


def blocks(conf: str):
    """Top-level-ish brace blocks: [(header line, body text)] for every `server` and `location`."""
    out = []
    for m in re.finditer(r"^\s*((?:server|location)[^{\n]*)\{", conf, re.M):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(conf[i], 0)
            i += 1
        out.append((m.group(1).strip(), conf[m.end():i - 1]))
    return out


class DomainAndNames(unittest.TestCase):
    def test_good_domains(self):
        for d in ("sirosid.dev", "sm.invalid", "a.b.example", "x-1.example"):
            self.assertEqual(check_domain(d), d)

    def test_bad_domains(self):
        for d in ("", "dev", "SIROSID.DEV", "sirosid.dev.", ".sirosid.dev", "sirosid..dev", "*.sirosid.dev",
                  "sirosid.dev:443", "sirosid.dev/x", "-a.dev", "a-.dev", "a_b.dev", "a" * 64 + ".dev",
                  "x.fly.dev", "fly.dev", "x.internal", "console.sirosid.dev", "www.sirosid.dev",
                  "sirosid.dev;app=x", 'a".dev', "a b.dev", None):
            with self.assertRaises(EdgeConfigError, msg=repr(d)):
                check_domain(d)
            with self.assertRaises(EdgeConfigError, msg=repr(d)):
                edge_nginx_conf(d)

    def test_the_edge_may_not_take_an_instance_name(self):
        for app in ("sid-edge", "sid-k7m2qx4d", "Edge", "", "-x"):
            with self.assertRaises(EdgeConfigError, msg=app):
                check_edge_app(app)
        self.assertEqual(check_edge_app("sbx-edge-x1y2z3"), "sbx-edge-x1y2z3")

    def test_components_match_the_single_machine_public_set(self):
        self.assertEqual(set(DEFAULT_COMPONENTS), set(PUBLIC_COMPONENTS))
        with self.assertRaises(EdgeConfigError):
            edge.instance_host_regex(D, ("console",))
        with self.assertRaises(EdgeConfigError):
            edge.instance_host_regex(D, ("vc-apigw;x",))


class MirrorCorpus(unittest.TestCase):
    def test_every_host_maps_exactly_as_expected(self):
        got = [(h, route(h, D)) for h, _, _ in CORPUS]
        want = [(h, (r, a)) for h, r, a in CORPUS]
        self.assertEqual(got, want)

    def test_a_failed_replay_is_a_503_never_another_replay(self):
        self.assertEqual(route(f"vc-apigw-{ID}.{D}", D, replay_failed=True), ("503", None))
        self.assertEqual(route("evil.example", D, replay_failed=True), ("404", None))

    def test_the_config_carries_the_same_regex(self):
        conf = edge_nginx_conf(D)
        host_re = edge.instance_host_regex(D)
        named = host_re.replace("^(", "^(?:", 1).replace(f"-({edge.INSTANCE_ID_RE})", f"-(?<iid>{edge.INSTANCE_ID_RE})", 1)
        self.assertIn('"~^1:' + named[1:] + '"', conf)
        self.assertIn(f'"~{edge.RAW_HOST_REGEX}" 1;', conf)


class ConfigShape(unittest.TestCase):
    conf = edge_nginx_conf(D)

    def test_fly_replay_is_never_sent_always(self):
        lines = self.conf.splitlines()
        hits = [i for i, ln in enumerate(lines) if "fly-replay" in ln and not ln.lstrip().startswith("#")]
        self.assertTrue(hits)
        for i in hits:
            for ln in lines[max(0, i - 3):i + 4]:
                self.assertNotIn("always", ln.split("#")[0], ln)
        for head, body in blocks(self.conf):
            if "add_header fly-replay" in body and head.startswith("location"):
                self.assertNotIn(" always", body, head)
        # exactly one place sets it, from the map
        self.assertEqual(len(re.findall(r"add_header\s+fly-replay\s+\$sirosid_edge_replay;", self.conf)), 1)
        self.assertEqual(len(re.findall(r"add_header\s+fly-replay", self.conf)), 1)

    def test_the_map_is_empty_by_default(self):
        m = re.search(r"map \"\$sirosid_edge_raw_ok:\$host\" \$sirosid_edge_replay \{(.*?)\n\}", self.conf, re.S)
        self.assertIn('default "";', m.group(1))
        self.assertIn(f"app=sid-$iid;timeout={edge.REPLAY_TIMEOUT};fallback=prefer_self", m.group(1))

    def test_the_static_server_serves_only_the_static_root(self):
        servers = [(h, b) for h, b in blocks(self.conf) if h.startswith("server")]
        static = [b for h, b in servers if f"server_name {D} www.{D};" in b]
        self.assertEqual(len(static), 1)
        body = static[0]
        self.assertEqual(re.findall(r"^\s*root\s+(\S+);", body, re.M), [edge.STATIC_ROOT])
        self.assertNotRegex(body, r"\balias\b")
        self.assertIn("default-src 'none'", body)
        self.assertIn("frame-ancestors 'none'", body)
        self.assertNotIn("fly-replay", body)
        self.assertNotIn("'unsafe-inline'", body)
        # the edge never proxies anything, anywhere
        self.assertNotRegex(self.conf, r"\b(proxy_pass|fastcgi_pass|uwsgi_pass|grpc_pass|scgi_pass)\b")

    def test_there_is_a_default_server_with_health_and_error_pages(self):
        self.assertEqual(self.conf.count("default_server"), 1)
        self.assertIn(f"location = {edge.HEALTH_PATH}", self.conf)
        self.assertIn("if ($http_fly_replay_failed != \"\")", self.conf)
        self.assertIn("return 503", self.conf)

    def test_fly_toml(self):
        t = edge_fly_toml("sbx-edge-x1y2z3", region="arn")
        self.assertIn('app = "sbx-edge-x1y2z3"', t)
        self.assertIn(f'path = "{edge.HEALTH_PATH}"', t)
        self.assertIn('auto_stop_machines = "off"', t)
        with self.assertRaises(EdgeConfigError):
            edge_fly_toml("sid-x")

    def test_dns_and_certs_are_printed_for_the_wildcard_and_apex(self):
        self.assertEqual(edge.cert_commands("sirosid.dev", "sirosid-edge"),
                         ["flyctl certs add sirosid.dev -a sirosid-edge", "flyctl certs add www.sirosid.dev -a sirosid-edge",
                          "flyctl certs add *.sirosid.dev -a sirosid-edge"])
        names = {n for n, _, _ in edge.dns_records("sirosid.dev", "sirosid-edge", "1.2.3.4", "::1")}
        self.assertEqual(names, {"sirosid.dev", "*.sirosid.dev", "www.sirosid.dev"})


class StaticSite(unittest.TestCase):
    def test_nothing_under_well_known_is_published_on_the_apex(self):
        """Chrome's Related Origin Requests: a served /.well-known/webauthn would let listed origins use
        the apex as their RP ID. The config answers 404 and the static root must have no such directory."""
        conf = edge_nginx_conf("sirosid.dev", ["wallet-frontend"])
        self.assertRegex(conf, r"location \^~ /\.well-known/ \{\s*return 404;")
        self.assertFalse((ROOT / "edge" / "site" / ".well-known").exists())

    def test_site_has_no_inline_code_or_external_assets(self):
        site = ROOT / "edge" / "site"
        files = {p.name: p.read_text() for p in site.iterdir()}
        self.assertIn("index.html", files)
        rendered = render_site(files, "sirosid.dev")
        self.assertIn('href="https://console.sirosid.dev/"', rendered["index.html"])
        self.assertIn("invite-only development instances", rendered["index.html"])
        for name, text in rendered.items():
            self.assertNotIn("@", text.replace("@media", ""), name)
            if name.endswith(".html"):
                self.assertNotRegex(text, r"(?i)<script|<style|\sstyle=|\son[a-z]+=|javascript:", name)
                for ref in re.findall(r'(?:src|href)="([^"]+)"', text):
                    self.assertTrue(ref.startswith("/") or ref == "https://console.sirosid.dev/", (name, ref))
            self.assertNotRegex(text, r"(?i)@import|url\(\s*['\"]?https?:", name)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _docker_ok():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", NGINX_IMAGE], capture_output=True, timeout=20).returncode == 0 \
            or subprocess.run(["docker", "pull", "-q", NGINX_IMAGE], capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class RealNginx(unittest.TestCase):
    """The generated config under real nginx: `nginx -t`, then the whole corpus."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="sirosid-edge-test-"))
        sys.path.insert(0, str(ROOT / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("edge_up", ROOT / "scripts" / "edge-up.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.build_context(cls.tmp, "sbx-edge-test01", D, "arn")
        cls.container = None
        cls.port = None
        if shutil.which("nginx") is None and not _docker_ok():
            raise unittest.SkipTest("neither an nginx binary nor docker with nginx:alpine is available")
        if _docker_ok():
            cls.port = _free_port()
            r = subprocess.run(["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{cls.port}:8080",
                                "-v", f"{cls.tmp / 'edge.conf'}:/etc/nginx/conf.d/default.conf:ro",
                                "-v", f"{cls.tmp / 'site'}:/srv/site:ro", NGINX_IMAGE],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise unittest.SkipTest(f"docker run failed: {r.stderr.strip()[:200]}")
            cls.container = r.stdout.strip()
            for _ in range(50):
                try:
                    if cls.get("/_edge/healthz", "x")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        if cls.container:
            subprocess.run(["docker", "rm", "-f", cls.container], capture_output=True, timeout=60)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def get(cls, path, host, headers=None, method="GET", body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def test_nginx_t(self):
        if self.container:
            r = subprocess.run(["docker", "exec", self.container, "nginx", "-t"], capture_output=True, text=True,
                               timeout=30)
        else:
            conf = self.tmp / "nginx.conf"
            conf.write_text(f"pid {self.tmp}/nginx.pid; error_log stderr; events {{}} "
                            f"http {{ include {self.tmp}/edge.conf; }}\n")
            r = subprocess.run(["nginx", "-t", "-c", str(conf), "-p", str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def _need_server(self):
        if not self.container:
            self.skipTest("no docker: nginx -t only")

    def test_the_corpus_against_real_nginx(self):
        self._need_server()
        got, want = [], []
        for host, expect, app in CORPUS:
            status, headers, _ = self.get("/x", host)
            replay = headers.get("fly-replay", "")
            if expect == "replay":
                want.append((host, 204, f"app={app};timeout={edge.REPLAY_TIMEOUT};fallback=prefer_self"))
            elif expect == "static":
                want.append((host, 404, ""))       # /x is not in the site
            else:
                want.append((host, int(expect), ""))
            got.append((host, status, replay))
        self.assertEqual(got, want)

    def test_failed_replay_gets_the_503_page_without_a_replay_header(self):
        self._need_server()
        status, headers, body = self.get("/", f"vc-apigw-{ID}.{D}", {"fly-replay-failed": "timeout"})
        self.assertEqual(status, 503)
        self.assertNotIn("fly-replay", headers)
        self.assertIn(b"Instance not available", body)
        self.assertIn("no-store", headers.get("cache-control", ""))

    def test_upgrades_and_large_bodies_are_left_to_the_replay(self):
        self._need_server()
        status, headers, _ = self.get("/ws", f"wallet-proxy-{ID}.{D}", {"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual((status, headers.get("fly-replay", "")[:16]), (204, f"app=sid-{ID}"))
        status, headers, _ = self.get("/up", f"vc-apigw-{ID}.{D}", method="POST", body=b"x" * (2 * 1024 * 1024))
        self.assertEqual(status, 204)
        self.assertIn("fly-replay", headers)

    def test_the_apex_is_the_static_site(self):
        self._need_server()
        status, headers, body = self.get("/", D)
        self.assertEqual(status, 200)
        self.assertIn(b"invite-only development instances", body)
        self.assertIn(b"https://console.sm.invalid/", body)
        self.assertNotIn("fly-replay", headers)
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(self.get("/site.css", f"www.{D}")[0], 200)
        self.assertEqual(self.get("/", D, method="POST", body=b"x")[0], 403)
        status, headers, _ = self.get("/nope", D)
        self.assertEqual(status, 404)
        self.assertIn("content-security-policy", headers)


if __name__ == "__main__":
    unittest.main()
