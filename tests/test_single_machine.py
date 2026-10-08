"""The single-machine layout (sirosid_core/singlemachine.py) against fake flyctl,
a fake Machines API and a fake registry (tests/fakemachines.py).

What these hold: the machine config fits the API's size cap with a real render
(the default one and gdc's), every component listens on its own port, nothing
points at another app (.internal), the front routes each public host to the
right container, secrets never travel in the config, a redeploy that changes
nothing does not reboot the machine, failures say where they stopped, and the
lifecycle (stop/start/reset/destroy) is one machine's worth of calls.
"""
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakefly import FakeFly  # noqa: E402
from fakemachines import FakeMachines  # noqa: E402
from sirosid_core import lifecycle  # noqa: E402
from sirosid_core.assets import FRONT_PASSTHROUGH, front_nginx_conf  # noqa: E402
from sirosid_core.deploy import DeployError, RegistrationError  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.machines import MachinesClient, MachinesError  # noqa: E402
from sirosid_core.naming import SINGLE_MACHINE_PORTS, Naming  # noqa: E402
from sirosid_core.oci import Registry, build_layer, image_blobs  # noqa: E402
from sirosid_core.resources import Resources  # noqa: E402
from sirosid_core.singlemachine import (MAX_CONFIG_BYTES, PUBLIC_COMPONENTS, block_devices,  # noqa: E402
                                        deploy_instance_single_machine, request_size, rewrite_paths)
from sirosid_core.spec import InstanceSpec  # noqa: E402

NEEDS = unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
TOKEN = "FlyV1 fm2_single-machine-test-token"
PATTERN = "{component}-{id}.sid.example"


def spec(**kw):
    base = dict(env="k7m2qx4d", region="arn", app_prefix="sid", host_pattern=PATTERN, env_admin=False,
                layout="single-machine")
    base.update(kw)
    return InstanceSpec(**base)


class Harness:
    def __init__(self, fake=None):
        self.fake = fake or FakeFly()
        self.api = FakeMachines(self.fake)
        ok = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0)  # noqa: E731
        self.messages = []
        self.fly = FlyClient(org="sandbox", token=TOKEN, runner=self.fake.runner(), docker=ok,
                             out=self.messages.append, err=self.messages.append)
        self.machines = MachinesClient(TOKEN, transport=self.api.transport, sleep=lambda s: None)
        self.registry = Registry("registry.fly.io", "x", TOKEN, transport=self.api.registry_transport)
        self.root = Path(tempfile.mkdtemp(prefix="smtest-"))

    def deploy(self, s=None, **kw):
        s = s or spec()
        kw.setdefault("progress", self.messages.append)
        kw.setdefault("registry", self.registry)
        return deploy_instance_single_machine(s, self.fly, s.naming(), Resources(ROOT), machines=self.machines,
                                              rendered_root=self.root, **kw)

    def config(self, s=None):
        return json.loads((self.root / f"fly-{(s or spec()).env}" / "machine-config.json").read_text())

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def files_of(container):
    return {f["guest_path"]: base64.b64decode(f["raw_value"]).decode("utf-8", "replace")
            for f in container.get("files", []) if "raw_value" in f}


def container(config, name):
    return next(c for c in config["containers"] if c["name"] == name)


def everything_as_text(config) -> str:
    """The config with every inlined file decoded - what a scan must cover."""
    parts = [json.dumps(config)]
    for c in config["containers"]:
        parts += list(files_of(c).values())
    return "\n".join(parts)


@NEEDS
class MachineConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.result = cls.h.deploy(register=lambda *a: {"issuer": "registered", "verifier": "registered"})
        cls.cfg = cls.h.config()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_one_app_one_machine(self):
        apps = [c.split()[3] for c in self.h.fake.log if " apps create " in c]
        self.assertEqual(apps, ["sid-k7m2qx4d"])
        self.assertEqual(len(self.h.api.machines["sid-k7m2qx4d"]), 1)
        self.assertEqual(self.result.machine["app"], "sid-k7m2qx4d")
        self.assertFalse([c for c in self.h.fake.log if c.startswith("flyctl deploy")], "no flyctl deploy at all")

    def test_fits_the_api_body_cap(self):
        size = request_size(self.cfg, "arn", 999)
        self.assertLess(size, MAX_CONFIG_BYTES)
        self.assertLess(size, 1024 * 1024)

    def test_fits_the_machines_block_devices(self):
        drives, temps = block_devices(self.cfg)
        self.assertLessEqual(drives, 12)
        self.assertLessEqual(drives + temps, 14)

    def test_every_listener_is_unique(self):
        ports = [p for kinds in SINGLE_MACHINE_PORTS.values() for p in kinds.values()]
        self.assertEqual(len(ports), len(set(ports)))
        # ... and the rendered configs actually use them.
        out = self.h.root / "fly-k7m2qx4d"
        reg = yaml.safe_load((out / "vc-registry.yaml").read_text())["registry"]
        iss = yaml.safe_load((out / "vc-issuer.yaml").read_text())["issuer"]
        ver = yaml.safe_load((out / "vc-verifier.yaml").read_text())["verifier"]
        gw = yaml.safe_load((out / "vc-apigw.yaml").read_text())["apigw"]
        pdp = yaml.safe_load((out / "pdp.yaml").read_text())["server"]
        wb = yaml.safe_load((out / "wallet-backend.yaml").read_text())["server"]
        bound = [reg["api_server"]["addr"], reg["grpc_server"]["addr"], iss["api_server"]["addr"],
                 iss["grpc_server"]["addr"], ver["api_server"]["addr"], gw["api_server"]["addr"],
                 f"{pdp['host']}:{pdp['port']}", f"{wb['host']}:{wb['port']}", f"{wb['host']}:{wb['admin_port']}",
                 f"{wb['host']}:{wb['engine_port']}"]
        self.assertEqual(len(bound), len(set(bound)), bound)
        self.assertTrue(all(b.startswith("127.0.0.1:") for b in bound), bound)
        checks = [hc.get("http", hc.get("tcp"))["port"] for c in self.cfg["containers"] for hc in c.get("healthchecks", [])]
        self.assertEqual(len(checks), len(set(checks)), checks)

    def test_nothing_points_at_another_app(self):
        text = everything_as_text(self.cfg)
        self.assertNotIn(".internal", text)
        self.assertNotIn(".fly.dev", text, "every public name comes from the host pattern")
        for f in ("vc-apigw.yaml", "vc-verifier.yaml", "vc-issuer.yaml", "vc-registry.yaml", "pdp.yaml",
                  "wallet-backend.yaml"):
            self.assertNotIn(".internal", (self.h.root / "fly-k7m2qx4d" / f).read_text(), f)

    def test_large_files_go_in_the_bundle_and_configs_point_there(self):
        vc = files_of(container(self.cfg, "vc-apigw"))["/config.yaml"]
        self.assertIn("/sirosid/bundle/vctms/", vc)
        self.assertIn("/sirosid/bundle/documents/", vc)
        self.assertNotRegex(vc, r"(?m)[: -] /vctms/")
        self.assertEqual(self.cfg["volumes"][0]["name"], "bundle")
        self.assertRegex(self.cfg["volumes"][0]["image"], r"^registry\.fly\.io/sid-k7m2qx4d@sha256:[0-9a-f]{64}$")
        self.assertTrue(any(k.startswith("sid-k7m2qx4d:cfg-") for k in self.h.api.manifests))

    def test_secrets_never_travel_in_the_config(self):
        out = self.h.root / "fly-k7m2qx4d"
        text = everything_as_text(self.cfg)
        for secret in ("adminToken", "jwtSecret"):
            self.assertNotIn((out / secret).read_text().strip(), text)
        for pem in ("signing_ec_private.pem", "wallet_provider_ec_private.pem"):
            body = "".join((out / "vc-pki" / pem).read_text().splitlines()[1:-1])
            self.assertNotIn(body[:40], text.replace("\n", ""))
        init = container(self.cfg, "secrets-init")
        self.assertEqual(init["restart"], {"policy": "no"})
        self.assertTrue({s["name"] for s in init["secrets"]} >= {"SIROSID_VC_SIGNING_KEY", "SIROSID_ADMIN_TOKEN"})
        # Only the init container sees the secrets; consumers wait for it.
        self.assertEqual([c["name"] for c in self.cfg["containers"] if c.get("secrets")], ["secrets-init"])
        wb = container(self.cfg, "wallet-backend")
        self.assertIn({"name": "secrets-init", "condition": "exited_successfully"}, wb["depends_on"])

    def test_order_is_depends_on(self):
        gw = {d["name"]: d["condition"] for d in container(self.cfg, "vc-apigw")["depends_on"]}
        self.assertEqual(gw.get("vc-issuer"), "healthy")
        self.assertEqual(gw.get("vc-registry"), "healthy")
        self.assertEqual(gw.get("mongodb"), "healthy")
        for name in ("wallet-backend", "vc-verifier", "vc-registry", "vc-issuer"):
            deps = {d["name"]: d["condition"] for d in container(self.cfg, name)["depends_on"]}
            self.assertEqual(deps.get("pdp"), "healthy", name)
            self.assertEqual(deps.get("mongodb"), "healthy", name)
        self.assertNotIn("depends_on", container(self.cfg, "wallet-frontend"),
                         "the front must answer within seconds of a start")

    def test_returns_what_a_caller_needs(self):
        self.assertEqual(self.result.urls["vc-apigw"], "https://vc-apigw-k7m2qx4d.sid.example")
        self.assertEqual(self.result.urls["fly.dev"], "https://sid-k7m2qx4d.fly.dev")
        self.assertEqual(len(self.result.admin_token), 32)
        self.assertEqual(set(self.result.machine["containers"]) >= {"mongodb", "wallet-frontend"}, True)
        self.assertIn("register", self.result.deployed)

    def test_the_token_is_only_ever_a_header(self):
        self.assertEqual(set(self.h.api.auth_seen), {TOKEN})
        self.assertTrue(self.h.api.registry_auth)
        self.assertNotIn(TOKEN, "\n".join(str(m) for m in self.h.messages))
        self.assertNotIn(TOKEN, everything_as_text(self.cfg))
        self.assertNotIn(TOKEN, repr(self.h.machines))

    def test_a_redeploy_that_changes_nothing_does_not_reboot(self):
        h = Harness()
        try:
            h.deploy()
            first = dict(h.api.only("sid-k7m2qx4d"))
            h.deploy()
            m = h.api.only("sid-k7m2qx4d")
            self.assertEqual(m["updates"], 0)
            self.assertEqual(m["instance_id"], first["instance_id"])
            creates = [c for c in h.fake.log if " volumes create " in c]
            self.assertEqual(len(creates), 1)
            self.assertEqual(len([c for c in h.fake.log if "ips allocate-v6" in c]), 1,
                             "allocate-v6 adds an address every call")
        finally:
            h.close()


@NEEDS
class GdcSizeTest(unittest.TestCase):
    """gdc is the heaviest real environment (trust lists, RICAL, ZK sources,
    its own values block): its single-machine config must still fit."""

    def test_gdc_fits(self):
        env = yaml.safe_load((ROOT / "environments" / "gdc.yaml").read_text())
        s = spec(env="gdcsized", credential_registries=env.get("credential_registries") or [],
                 trusted_issuers=env.get("trusted_issuers") or [], trusted_verifiers=env.get("trusted_verifiers") or [],
                 trusted_verifier_roots=[(ROOT / p).read_text() for p in env.get("trusted_verifier_roots") or []],
                 zk_circuits_sources=env.get("zk_circuits_sources") or [], android_apps=env.get("android_apps") or [],
                 wallet_attestation=bool(env.get("wallet_attestation")),
                 rical_provider_url=env.get("rical_provider_url") or "",
                 rical_root_pem=(ROOT / env["rical_root_cert"]).read_text() if env.get("rical_root_cert") else "",
                 values=env.get("values") or {})
        h = Harness()
        try:
            r = h.deploy(s, render_only=True)
            size = r.machine["config_bytes"]
            self.assertLess(size, MAX_CONFIG_BYTES, size)
            self.assertGreater(size, 50_000, "a real render, not an empty one")
            self.assertTrue(any("geneva2026" in c for c in s.trusted_issuers + [s.rical_provider_url]))
            self.assertNotIn(".internal", everything_as_text(h.config(s)))
        finally:
            h.close()


@NEEDS
class RoutingTests(unittest.TestCase):
    N = Naming("k7m2qx4d", app_prefix="sid", host_pattern=PATTERN, layout="single-machine")

    def servers(self, text):
        return re.findall(r"server \{(.*?)\n\}", text, re.S)

    def test_one_server_name_per_public_host_to_its_own_port(self):
        conf = front_nginx_conf(self.N)
        blocks = self.servers(conf)
        names = {}
        for b in blocks:
            m = re.search(r"server_name (\S+);", b)
            if m:
                names[m.group(1)] = b
        for comp in FRONT_PASSTHROUGH:
            host = self.N.host(comp)
            self.assertIn(host, names, comp)
            self.assertIn(f"proxy_pass http://127.0.0.1:{self.N.port(comp)};", names[host], comp)
        wp = names[self.N.host("wallet-proxy")]
        self.assertIn("default_server", wp, "<app>.fly.dev answers as wallet-proxy")
        self.assertIn("proxy_pass http://127.0.0.1:8111;", wp)
        self.assertIn(f"listen {self.N.listen('wallet-proxy')};", wp)
        self.assertEqual(set(PUBLIC_COMPONENTS), set(FRONT_PASSTHROUGH) | {"wallet-proxy"})

    def test_behind_an_edge_unknown_hosts_are_refused_and_edge_headers_trusted(self):
        conf = front_nginx_conf(self.N, behind_edge=True)
        blocks = self.servers(conf)
        default = [b for b in blocks if "default_server" in b]
        self.assertEqual(len(default), 1)
        self.assertIn("return 421;", default[0])
        self.assertIn("server_name _;", default[0])
        self.assertIn("X-Real-IP $http_fly_client_ip", conf)
        # A client's X-Forwarded-Proto survives Fly's proxy (real Fly, 2026-10-05): never pass it on.
        self.assertNotIn("$http_x_forwarded_proto", conf)
        self.assertIn("X-Forwarded-Proto https;", conf)
        self.assertNotIn("$http_x_forwarded_proto", front_nginx_conf(self.N))
        # IPv4 only: 6PN neighbours on the default network (IPv6) must not reach it.
        listens = re.findall(r"^\s*listen\s+([^;]+);", conf, re.M)
        self.assertTrue(listens)
        for listen in listens:
            self.assertNotIn("[", listen, "an IPv6 listener would be reachable from every app on the org network")
            self.assertNotIn("ipv6only", listen)

    def test_flat_host_pattern_is_required(self):
        self.assertEqual(self.N.flat_host_problem(), "")
        self.assertEqual(self.N.host("wallet-frontend"), "wallet-frontend-k7m2qx4d.sid.example")
        for bad in ("{component}.{id}.sid.example", "{app}.fly.dev", "{component}-{id}.fly.dev", "x.{component}.example"):
            self.assertTrue(Naming("k7m2qx4d", host_pattern=bad, layout="single-machine").flat_host_problem(), bad)


@NEEDS
class RefusalsAndFailures(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_unsupported_shapes_are_refused_up_front(self):
        for s, word in ((spec(env_admin=True), "env-admin"), (spec(conformance=True), "conformance"),
                        (spec(host_pattern="{app}.fly.dev"), "first label")):
            with self.assertRaises(DeployError) as cm:
                self.h.deploy(s)
            self.assertIn(word, str(cm.exception))
        self.assertEqual([c for c in self.h.fake.log if " apps create " in c], [])

    def test_a_failed_machine_create_says_where_it_stopped(self):
        self.h.api.fail["POST /apps/[^/]+/machines$"] = (422, {"error": "bad config"})
        with self.assertRaises(DeployError) as cm:
            self.h.deploy()
        self.assertEqual(cm.exception.component, "machine")
        self.assertIn("bundle", cm.exception.deployed)
        self.assertIn("secrets", cm.exception.deployed)

    def test_a_registration_that_never_succeeds_fails_the_deploy(self):
        def never(*a):
            raise RegistrationError("not yet")
        import sirosid_core.deploy as deploy_mod
        orig = deploy_mod.time.sleep
        deploy_mod.time.sleep = lambda s: None
        try:
            with self.assertRaises(DeployError) as cm:
                self.h.deploy(register=never)
        finally:
            deploy_mod.time.sleep = orig
        self.assertEqual(cm.exception.component, "register")
        self.assertIn("containers", cm.exception.deployed)

    def test_too_many_block_devices_is_refused_before_fly_sees_it(self):
        from sirosid_core.singlemachine import check_block_devices
        many = {"containers": [{"name": f"c{i}", "image": f"img{i}"} for i in range(12)],
                "mounts": [{"volume": "vol_x", "path": "/d"}]}
        with self.assertRaises(DeployError) as cm:
            check_block_devices(many)
        self.assertIn("block devices", str(cm.exception))
        same = {"containers": [{"name": f"c{i}", "image": "one"} for i in range(12)], "mounts": [{"volume": "v"}]}
        check_block_devices(same)   # one image shared by every container is one device

    def test_a_volume_without_the_cached_password_is_read_back_from_the_container(self):
        self.h.deploy()
        out = self.h.root / "fly-k7m2qx4d"
        pw = (out / "mongoRootPassword").read_text()
        (out / "mongoRootPassword").unlink()
        self.h.api.exec_result = {"stdout": pw + "\n", "stderr": "", "exit_code": 0}
        self.h.deploy()
        self.assertEqual((out / "mongoRootPassword").read_text(), pw)
        execs = self.h.api.only("sid-k7m2qx4d")["execs"]
        self.assertEqual(execs[-1]["container"], "mongodb")

    def test_an_unreadable_password_with_data_stops_the_deploy(self):
        self.h.deploy()
        (self.h.root / "fly-k7m2qx4d" / "mongoRootPassword").unlink()
        self.h.api.exec_result = {"stdout": "", "stderr": "no", "exit_code": 1}
        with self.assertRaises(DeployError) as cm:
            self.h.deploy()
        self.assertIn("lock every consumer out", str(cm.exception))


@NEEDS
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.h.deploy()
        self.n = spec().naming()
        self.app = self.n.machine_app()

    def test_stop_and_start_are_one_machine(self):
        r = lifecycle.stop_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertTrue(r.ok)
        self.assertEqual(self.h.api.only(self.app)["state"], "stopped")
        r = lifecycle.start_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertTrue(r.ok, r.failed)
        self.assertEqual(r.changed, [self.app])
        self.assertEqual(self.h.api.only(self.app)["state"], "started")

    def test_stopped_means_cordoned_so_an_edge_replay_cannot_wake_it(self):
        """Real Fly: a replay starts a stopped machine even with autostart off; only a
        cordon keeps it stopped (the edge then shows its 503 page)."""
        machine = self.h.api.only(self.app)
        lifecycle.stop_instance(self.h.fly, self.n, machines=self.h.machines)
        log = [c for c in self.h.api.log if c.endswith(("/cordon", "/stop"))]
        self.assertEqual([c.rsplit("/", 1)[1] for c in log[-2:]], ["cordon", "stop"], "cordon before the stop")
        self.assertTrue(machine["cordoned"])
        lifecycle.start_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertFalse(machine["cordoned"])
        self.assertTrue(self.h.api.log[-1].endswith("/uncordon"), "routable only once the containers are up")
        # a deploy over a stopped (cordoned) instance leaves it reachable
        lifecycle.stop_instance(self.h.fly, self.n, machines=self.h.machines)
        self.h.deploy()
        self.assertFalse(machine["cordoned"])
        # and so does a reset
        lifecycle.stop_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertTrue(lifecycle.reset_single_machine(self.h.fly, self.h.machines, self.n).ok)
        self.assertFalse(machine["cordoned"])

    def test_a_start_that_never_gets_healthy_still_uncordons(self):
        lifecycle.stop_instance(self.h.fly, self.n, machines=self.h.machines)
        self.h.api.fail["GET /apps/[^/]+/machines/[^/]+$"] = (500, {"error": "boom"})
        r = lifecycle.start_instance(self.h.fly, self.n, machines=self.h.machines, ready_timeout=0)
        self.assertFalse(r.ok)
        self.assertFalse(self.h.api.only(self.app)["cordoned"])

    def test_reset_drops_data_in_place_and_restarts(self):
        before = self.h.api.log[:]
        r = lifecycle.reset_single_machine(self.h.fly, self.h.machines, self.n)
        self.assertTrue(r.ok, r.failed)
        calls = self.h.api.log[len(before):]
        self.assertTrue(any(c.endswith("/exec") for c in calls))
        self.assertTrue(any(c.endswith("/stop") for c in calls) and any(c.endswith("/start") for c in calls))
        ex = self.h.api.only(self.app)["execs"][-1]
        self.assertEqual(ex["container"], "mongodb")
        self.assertIn("dropDatabase", ex["command"][-1])
        self.assertIn('"admin","local","config"', ex["command"][-1])
        self.assertTrue(self.h.fly.app_exists(self.app), "a reset keeps the instance")

    def test_a_failed_drop_is_reported(self):
        self.h.api.exec_result = {"stdout": "", "stderr": "auth failed", "exit_code": 1}
        r = lifecycle.reset_single_machine(self.h.fly, self.h.machines, self.n)
        self.assertFalse(r.ok)
        self.assertIn("auth failed", r.failed[0][1])

    def test_destroy_is_one_app_and_keep_data_stops_instead(self):
        r = lifecycle.destroy_instance(self.h.fly, self.n, keep_data=True, machines=self.h.machines)
        self.assertEqual(r.kept, [self.app])
        self.assertEqual(self.h.api.only(self.app)["state"], "stopped")
        r = lifecycle.destroy_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertEqual(r.destroyed, [self.app])
        self.assertFalse(self.h.fly.app_exists(self.app))
        r = lifecycle.destroy_instance(self.h.fly, self.n, machines=self.h.machines)
        self.assertEqual(r.absent, [self.app])

    def test_works_without_a_machines_client_too(self):
        r = lifecycle.destroy_instance(self.h.fly, self.n)
        self.assertEqual(r.destroyed, [self.app])

    def test_the_apps_layout_naming_never_names_the_single_app(self):
        apps = Naming("k7m2qx4d", app_prefix="sid")
        from sirosid_core.components import component_names
        self.assertNotIn(self.app, [apps.app(c) for c in component_names()])


class PureParts(unittest.TestCase):
    def test_rewrite_paths_only_touches_whole_path_prefixes(self):
        cfg = {"a": "/vctms/x.json", "b": ["/documents/d.json", "/documentsX"], "c": {"d": "/vctms"}, "e": 3,
               "f": "keep /vctms/ in prose"}
        out = rewrite_paths(cfg, {"/vctms": "/B/vctms", "/documents": "/B/documents"})
        self.assertEqual(out, {"a": "/B/vctms/x.json", "b": ["/B/documents/d.json", "/documentsX"],
                               "c": {"d": "/B/vctms"}, "e": 3, "f": "keep /vctms/ in prose"})

    def test_the_bundle_layer_is_deterministic(self):
        files = {"vctms/a.json": b"{}", "documents/d.json": b"[]"}
        self.assertEqual(image_blobs(files)["digest"], image_blobs(dict(reversed(list(files.items()))))["digest"])
        with self.assertRaises(ValueError):
            build_layer({"../escape": b"x"})

    def test_machines_errors_never_carry_the_request_body(self):
        def transport(method, url, headers, body, timeout):
            return 500, b'{"error":"boom","value":"s3cret"}'
        mc = MachinesClient(TOKEN, transport=transport)
        with self.assertRaises(MachinesError) as cm:
            mc.set_secret("a", "N", "s3cret")
        self.assertNotIn("s3cret", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_reads_are_retried_on_network_errors_writes_are_not(self):
        calls = []

        def flaky(method, url, headers, body, timeout):
            calls.append(method)
            if len(calls) < 3:
                raise OSError("Temporary failure in name resolution")
            return 200, b"[]"
        mc = MachinesClient(TOKEN, transport=flaky, sleep=lambda s: None)
        self.assertEqual(mc.list_machines("a"), [])
        calls.clear()
        with self.assertRaises(MachinesError):
            mc.stop_machine("a", "m")
        self.assertEqual(calls, ["POST"])


if __name__ == "__main__":
    unittest.main()
