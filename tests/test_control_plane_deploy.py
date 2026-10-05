"""The control plane's deployment (deploy/control-plane/, the two workflows, the root
.dockerignore, scripts/rotate_fly_token.py): the parts that can be checked without
Docker or Fly.

- every path the Dockerfile COPYs exists, survives the .dockerignore, and the image
  tree it produces is enough for a real deploy_instance (against the fake flyctl);
- generated secrets under allowed directories never reach the build context;
- downloaded binaries are pinned by version and sha256, and the workflows pin the
  same flyctl as the image;
- fly.toml has the settings the service depends on (one always-on machine, /data
  volume, /healthz check, the env the service reads);
- the Litestream config is generated from the environment and never contains a key;
- the entrypoint restores fail-closed and only starts empty with the first-boot flag;
- workflows parse and pin every action to a full commit SHA.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
DEPLOY = ROOT / "deploy" / "control-plane"
DOCKERFILE = DEPLOY / "Dockerfile"
WORKFLOWS = [ROOT / ".github" / "workflows" / n for n in ("deploy-control-plane.yml", "rotate-fly-org-token.yml")]

sys.path.insert(0, str(DEPLOY))
import litestream_config  # noqa: E402


# ---- Dockerfile / .dockerignore ------------------------------------------------------

def dockerfile_copies(path=DOCKERFILE):
    """[(sources, dest)] of every COPY that reads the build context (not --from)."""
    text = re.sub(r"\\\n", " ", path.read_text())
    out = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].upper() != "COPY":
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        if any(p.startswith("--from") for p in parts[1:]):
            continue
        out.append((args[:-1], args[-1]))
    return out


def _glob_re(pattern):
    """Docker/Go filepath.Match semantics, plus ** for any number of directories."""
    i, out = 0, ""
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += r"(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += r".*"; i += 2
        elif pattern[i] == "*":
            out += r"[^/]*"; i += 1
        elif pattern[i] == "?":
            out += r"[^/]"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(out + r"$")


def dockerignore_rules(path=ROOT / ".dockerignore"):
    rules = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:] if neg else line
        rules.append((neg, _glob_re(pat.strip("/"))))
    return rules


def excluded(rel: str, rules) -> bool:
    """Last matching rule wins; a rule matching a parent directory covers its children."""
    parts = rel.strip("/").split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    state = False
    for neg, rx in rules:
        if any(rx.match(c) for c in candidates):
            state = not neg
    return state


def context_files(rules):
    """Every file under the repo that would be sent as build context."""
    for p in ROOT.rglob("*"):
        if p.is_file() and ".git" not in p.relative_to(ROOT).parts[:1]:
            rel = p.relative_to(ROOT).as_posix()
            if not excluded(rel, rules):
                yield rel


class DockerfileTests(unittest.TestCase):
    def test_every_copied_path_exists_and_survives_the_dockerignore(self):
        rules = dockerignore_rules()
        copies = dockerfile_copies() + dockerfile_copies(ROOT / "env-admin" / "Dockerfile")
        self.assertGreater(len(copies), 5)
        for sources, _ in copies:
            for s in sources:
                self.assertTrue((ROOT / s).exists(), f"COPY source {s} does not exist")
                self.assertFalse(excluded(s, rules), f"COPY source {s} is excluded by .dockerignore")

    def test_generated_secrets_never_reach_the_build_context(self):
        rules = dockerignore_rules()
        for rel in ["fixtures/rendered/fly-x/mongoRootPassword", "fixtures/rendered-secrets/apiAuthKey.pem",
                    "fixtures/vc-pki/signing_ec_private.key", "fixtures/vc-pki/rootCA.crt", "fixtures/wrpac-pki/ca.key",
                    "fixtures/wrpac-clients/a.key", ".env", ".env.golden", ".android-apps", ".fly-region",
                    "environments/gdc.yaml", ".venv/bin/python", ".git/config", "tests/test_api.py",
                    "sirosid_service/__pycache__/api.cpython-313.pyc", "console/test/container.test.mjs", ".claude/x"]:
            self.assertTrue(excluded(rel, rules), f"{rel} would be sent to the builder")
        # ...while what is checked in under the same directories still goes.
        for rel in ["fixtures/trusted-roots/multipaz-reader-ca.pem", "fixtures/create-pki.sh", "fixtures/vc-pki/rootCA.srl"]:
            self.assertFalse(excluded(rel, rules), rel)

    def test_the_build_context_is_only_what_the_images_need(self):
        sent = set(context_files(dockerignore_rules()))
        tops = {r.split("/")[0] for r in sent}
        self.assertLessEqual(tops, {"chart", "console", "deploy", "env-admin", "fixtures", "scripts", "sirosid_core",
                                    "sirosid_service", "values-base.yaml", "values-dev.yaml", "values-fly.yaml"}, tops)
        self.assertNotIn("scripts/fly-up.py", sent)

    def test_downloads_are_pinned_by_version_and_sha256_and_verified(self):
        text = DOCKERFILE.read_text()
        args = dict(re.findall(r"^ARG (\w+)=(\S+)", text, re.M))
        for tool in ("HELM", "FLYCTL", "LITESTREAM"):
            self.assertRegex(args[f"{tool}_VERSION"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(args[f"{tool}_SHA256"], r"^[0-9a-f]{64}$")
            self.assertIn(f'echo "${{{tool}_SHA256}}', text, f"{tool} download is not checked")
        self.assertIn("sha256sum -c -", text)
        self.assertRegex(args["PYTHON_IMAGE"], r"@sha256:[0-9a-f]{64}$", "the base image is pinned by digest")
        self.assertIn("--require-hashes", text)

    def test_the_lock_pins_every_requirement_with_hashes(self):
        lock = (DEPLOY / "requirements.lock").read_text()
        pins = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\$", lock, re.M)
        names = {n.lower().replace("_", "-") for n, _ in pins}
        for need in ("cryptography", "starlette", "uvicorn", "webauthn", "pyyaml"):
            self.assertIn(need, names)
        self.assertEqual(len(re.findall(r"^\S+==", lock, re.M)), len(pins))
        self.assertGreaterEqual(lock.count("--hash=sha256:"), len(pins))
        ranges = (ROOT / "sirosid_service" / "requirements.txt").read_text()
        for name in ("cryptography", "starlette", "uvicorn", "webauthn"):
            self.assertIn(name, ranges, "the lock and the service's declared requirements name the same packages")

    def test_the_service_runs_unprivileged(self):
        text = DOCKERFILE.read_text()
        self.assertIn("useradd --system --uid 10001", text)
        ep = (DEPLOY / "entrypoint.sh").read_text()
        self.assertIn('exec setpriv --reuid="$RUN_AS"', ep)
        self.assertIn("setpriv --reuid=app", (DEPLOY / "sirosid-admin").read_text())


@unittest.skipUnless(shutil.which("helm") and shutil.which("openssl"), "needs helm and openssl")
class ImageTreeTests(unittest.TestCase):
    """Copy exactly what the Dockerfile copies (minus the .dockerignore) into a fresh
    tree and run a real deploy against it: proves Resources(root=/app) is complete."""

    def test_a_deploy_from_the_image_tree_alone_works(self):
        from fakefly import FakeFly
        from sirosid_core.deploy import deploy_instance
        from sirosid_core.fly import FlyClient
        from sirosid_core.naming import Naming
        from sirosid_core.policy import PlatformPolicy, build_spec
        from sirosid_core.resources import Resources

        rules = dockerignore_rules()
        tree = Path(tempfile.mkdtemp(prefix="cp-image-"))
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        for sources, dest in dockerfile_copies():
            if not dest.startswith("/app"):
                continue
            for s in sources:
                src = ROOT / s
                target = tree / dest[len("/app/"):] if dest.endswith("/") else tree / dest[len("/app/"):]
                if src.is_dir():
                    for p in src.rglob("*"):
                        rel = p.relative_to(ROOT).as_posix()
                        if p.is_file() and not excluded(rel, rules):
                            out = target / p.relative_to(src)
                            out.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(p, out)
                else:
                    out = target / src.name if dest.endswith("/") else target
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, out)
        self.assertEqual(Resources(tree).missing(), [])
        platform = PlatformPolicy(region="arn", app_prefix="sid", host_pattern="{app}.fly.dev", scale_to_zero=True,
                                  env_admin=False)
        spec = build_spec({}, "k7m2qx4d", frozenset(), platform)
        fake = FakeFly()
        ok = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0)
        fly = FlyClient(org="sandbox", token="FlyV1 t", runner=fake.runner(), docker=ok, out=lambda m: None, err=lambda m: None)
        scratch = Path(tempfile.mkdtemp(prefix="cp-render-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        result = deploy_instance(spec, fly, spec.naming(), Resources(tree), rendered_root=scratch, progress=lambda m: None)
        self.assertIn("wallet-frontend", result.urls)
        self.assertTrue(any(c.startswith("flyctl deploy") for c in fake.log))
        leaked = [c for c in fake.log if str(ROOT) in c]
        self.assertEqual(leaked, [], "the deploy reached outside the image tree")


# ---- fly.toml ----------------------------------------------------------------------

class FlyTomlTests(unittest.TestCase):
    def setUp(self):
        self.t = tomllib.loads((DEPLOY / "fly.toml").read_text())

    def test_one_always_on_machine_with_a_volume_and_a_health_check(self):
        t = self.t
        self.assertEqual(t["primary_region"], "arn")
        svc = t["http_service"]
        self.assertEqual(svc["internal_port"], 8080)
        self.assertTrue(svc["force_https"])
        self.assertEqual(svc["auto_stop_machines"], "off", "scale to zero would stop the reaper and sweeper")
        self.assertGreaterEqual(svc["min_machines_running"], 1)
        self.assertEqual([c["path"] for c in svc["checks"]], ["/healthz"])
        self.assertEqual(t["mounts"][0]["destination"], "/data")
        self.assertEqual(t["mounts"][0]["initial_size"], "1gb")

    def test_env_is_what_the_service_reads_and_holds_no_secret(self):
        env = self.t["env"]
        self.assertEqual(env["SIROSID_DB"], "/data/sirosid.db")
        self.assertEqual(env["SIROSID_CLIENT_IP_HEADER"], "Fly-Client-IP")
        self.assertEqual(env["SIROSID_FLY_ORG"], "sirosdev")
        for k in ("SIROSID_ORIGINS", "SIROSID_RP_ID", "SIROSID_APP_PREFIX", "SIROSID_HOST_PATTERN"):
            self.assertIn(k, env)
        for k in env:
            self.assertNotRegex(k, r"TOKEN|SECRET|ACCESS_KEY|PASSWORD|OPENROUTER", f"{k} is a secret; use fly secrets")
        self.assertNotIn("sirosid.dev", env["SIROSID_HOST_PATTERN"], "no instance content under the RP ID")
        self.assertNotRegex((DEPLOY / "fly.toml").read_text(), r"FlyV1|fm2_")

    def test_the_settings_load(self):
        from sirosid_service.config import Settings
        env = {**self.t["env"], "FLY_API_TOKEN": "x"}
        old = dict(os.environ)
        try:
            os.environ.clear(); os.environ.update(env)
            s = Settings.from_env()
        except ImportError:
            self.skipTest("service deps")
        finally:
            os.environ.clear(); os.environ.update(old)
        self.assertEqual((s.rp_id, s.origins, s.db_path, s.sweep_grace_seconds), (
            "sirosid.dev", ("https://console.sirosid.dev",), "/data/sirosid.db", 3600.0))


# ---- Litestream config + entrypoint ---------------------------------------------------

TIGRIS = {"BUCKET_NAME": "cp-backups", "AWS_ENDPOINT_URL_S3": "https://fly.storage.tigris.dev", "AWS_REGION": "auto",
          "LITESTREAM_ACCESS_KEY_ID": "tid_SECRETKEYID", "LITESTREAM_SECRET_ACCESS_KEY": "tsec_SECRETVALUE"}


class LitestreamConfigTests(unittest.TestCase):
    def test_tigris_settings_make_an_s3_replica_without_any_key_in_the_file(self):
        code, text = litestream_config.render(TIGRIS)
        self.assertEqual(code, 0)
        self.assertNotIn("SECRETKEYID", text)
        self.assertNotIn("SECRETVALUE", text)
        cfg = yaml.safe_load(text)
        rep = cfg["dbs"][0]["replica"]
        self.assertEqual(cfg["dbs"][0]["path"], "/data/sirosid.db")
        self.assertEqual((rep["type"], rep["bucket"], rep["endpoint"], rep["region"], rep["path"]),
                         ("s3", "cp-backups", "https://fly.storage.tigris.dev", "auto", "sirosid.db"))
        self.assertEqual(rep["access-key-id"], "${LITESTREAM_ACCESS_KEY_ID}")
        self.assertEqual(rep["secret-access-key"], "${LITESTREAM_SECRET_ACCESS_KEY}")

    def test_nothing_configured_is_its_own_answer(self):
        self.assertEqual(litestream_config.render({})[0], litestream_config.NOT_CONFIGURED)

    def test_half_a_replica_is_refused(self):
        for env in ({"BUCKET_NAME": "b"}, {**TIGRIS, "LITESTREAM_SECRET_ACCESS_KEY": ""},
                    {"LITESTREAM_REPLICA_URL": "s3://b/db"}):
            self.assertEqual(litestream_config.render(env)[0], litestream_config.INCOMPLETE, env)

    def test_an_s3_url_replica_references_the_keys_and_never_contains_them(self):
        code, text = litestream_config.render({"LITESTREAM_REPLICA_URL": "s3://b/db?endpoint=https://e.example",
                                               "LITESTREAM_ACCESS_KEY_ID": "tid_SECRETKEYID",
                                               "LITESTREAM_SECRET_ACCESS_KEY": "tsec_SECRETVALUE"})
        self.assertEqual(code, 0)
        self.assertNotIn("SECRETKEYID", text)
        self.assertNotIn("SECRETVALUE", text)
        self.assertEqual(yaml.safe_load(text)["dbs"][0]["replica"]["access-key-id"], "${LITESTREAM_ACCESS_KEY_ID}")

    def test_a_url_replica(self):
        code, text = litestream_config.render({"LITESTREAM_REPLICA_URL": "file:///backup/db", "SIROSID_DB": "/x/y.db"})
        self.assertEqual(code, 0)
        self.assertEqual(yaml.safe_load(text)["dbs"][0], {"path": "/x/y.db", "replica": {"url": "file:///backup/db"}})

    def test_the_cli_maps_aws_names_and_writes_no_secret(self):
        out = Path(tempfile.mkdtemp()) / "ls.yml"
        self.addCleanup(shutil.rmtree, out.parent)
        env = {k: v for k, v in TIGRIS.items() if not k.startswith("LITESTREAM_")}
        env.update(AWS_ACCESS_KEY_ID="AKIDSECRET", AWS_SECRET_ACCESS_KEY="AWSSECRETVALUE", PATH=os.environ["PATH"])
        r = subprocess.run([sys.executable, str(DEPLOY / "litestream_config.py"), str(out)], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for secret in ("AKIDSECRET", "AWSSECRETVALUE"):
            self.assertNotIn(secret, out.read_text())
            self.assertNotIn(secret, r.stdout + r.stderr)


@unittest.skipIf(os.geteuid() == 0, "the entrypoint's unprivileged half is tested as a normal user")
class EntrypointTests(unittest.TestCase):
    """entrypoint.sh with a stub `litestream` and a stub service command."""

    def run_ep(self, env, db_exists=False, restore_rc=0, restore_creates=True):
        d = Path(tempfile.mkdtemp(prefix="cp-ep-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        log = d / "calls"
        stub = d / "litestream"
        stub.write_text(f"""#!/bin/sh
echo "litestream $*" >> {log}
if [ "$1" = restore ]; then
  eval last=\\${{$#}}
  if [ {restore_rc} = 0 ] && [ {1 if restore_creates else 0} = 1 ]; then : > "$last"; fi
  exit {restore_rc}
fi
exit 0
""")
        stub.chmod(0o755)
        db = d / "data" / "sirosid.db"
        db.parent.mkdir()
        if db_exists:
            db.write_text("")
        full = {"PATH": os.environ["PATH"], "SIROSID_DB": str(db), "LITESTREAM_BIN": str(stub), "PYTHON": sys.executable,
                "LITESTREAM_CONFIG": str(d / "ls.yml"), "SIROSID_SERVE_CMD": f"sh -c 'echo serve >> {log}'", **env}
        r = subprocess.run(["sh", str(DEPLOY / "entrypoint.sh")], env=full, capture_output=True, text=True, timeout=30)
        calls = log.read_text().splitlines() if log.exists() else []
        return r, calls, d

    def test_no_replica_runs_the_service_directly_with_a_warning(self):
        r, calls, _ = self.run_ep({})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, ["serve"])
        self.assertIn("NOT backed up", r.stderr)

    def test_no_replica_is_refused_when_one_is_required(self):
        r, calls, _ = self.run_ep({"SIROSID_REQUIRE_REPLICA": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(calls, [])

    def test_a_half_configured_replica_never_runs_the_service(self):
        r, calls, _ = self.run_ep({"BUCKET_NAME": "b"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(calls, [])

    def test_missing_database_is_restored_then_replicated(self):
        r, calls, d = self.run_ep(TIGRIS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(calls[0].startswith("litestream restore -config "), calls)
        self.assertNotIn("-if-replica-exists", calls[0], "without the first-boot flag a missing replica is an error")
        self.assertTrue(calls[1].startswith("litestream replicate -config "))
        self.assertIn("-exec", calls[1])
        self.assertNotIn("SECRETVALUE", (d / "ls.yml").read_text())

    def test_a_failed_restore_stops_the_boot(self):
        r, calls, _ = self.run_ep(TIGRIS, restore_rc=1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SIROSID_FIRST_BOOT", r.stderr)
        self.assertFalse(any(c.startswith("litestream replicate") for c in calls), calls)

    def test_first_boot_tolerates_only_a_missing_replica(self):
        r, calls, _ = self.run_ep({**TIGRIS, "SIROSID_FIRST_BOOT": "1"}, restore_creates=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-if-replica-exists", calls[0])
        self.assertIn("starting with an empty database", r.stderr)
        r, calls, _ = self.run_ep({**TIGRIS, "SIROSID_FIRST_BOOT": "1"}, restore_rc=1)
        self.assertNotEqual(r.returncode, 0, "a real restore error is fatal even on first boot")

    def test_an_existing_database_is_not_restored_over(self):
        r, calls, _ = self.run_ep(TIGRIS, db_exists=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("litestream replicate"))


# ---- workflows -------------------------------------------------------------------------

def _uses(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "uses":
                yield v
            else:
                yield from _uses(v)
    elif isinstance(node, list):
        for v in node:
            yield from _uses(v)


class WorkflowTests(unittest.TestCase):
    def load(self, p):
        return yaml.safe_load(p.read_text())

    def test_they_parse_and_pin_every_action_to_a_commit_sha(self):
        for p in WORKFLOWS:
            wf = self.load(p)
            self.assertIn("jobs", wf, p.name)
            uses = list(_uses(wf))
            self.assertTrue(uses, p.name)
            for u in uses:
                self.assertRegex(u, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", f"{p.name}: {u} is not pinned to a full SHA")

    def test_minimal_permissions_and_a_protected_environment(self):
        for p in WORKFLOWS:
            wf = self.load(p)
            self.assertEqual(wf["permissions"], {"contents": "read"}, p.name)
            for job in wf["jobs"].values():
                self.assertTrue(job.get("environment"), f"{p.name}: the job needs a protected environment")
                self.assertNotIn("permissions", job)

    def test_triggers(self):
        deploy, rotate = (self.load(p) for p in WORKFLOWS)
        on = deploy.get("on", deploy.get(True))         # YAML 1.1 reads a bare `on` as True
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(on["push"], {"tags": ["control-plane-v*"]})
        on = rotate.get("on", rotate.get(True))
        self.assertIn("workflow_dispatch", on)
        self.assertTrue(on["schedule"][0]["cron"])

    def test_deploy_is_remote_only_with_the_app_scoped_token(self):
        text = WORKFLOWS[0].read_text()
        self.assertIn("--remote-only", text)
        self.assertIn("secrets.FLY_DEPLOY_TOKEN_CONTROL_PLANE", text)
        self.assertNotIn("docker build", text)
        self.assertIn("secrets.FLY_ROTATOR_TOKEN", WORKFLOWS[1].read_text())

    def test_the_workflows_install_the_same_flyctl_as_the_image(self):
        args = dict(re.findall(r"^ARG (\w+)=(\S+)", DOCKERFILE.read_text(), re.M))
        for p in WORKFLOWS:
            env = self.load(p)["env"]
            self.assertEqual((env["FLYCTL_VERSION"], env["FLYCTL_SHA256"]), (args["FLYCTL_VERSION"], args["FLYCTL_SHA256"]), p.name)
            self.assertIn("sha256sum -c -", p.read_text())


if __name__ == "__main__":
    unittest.main()
