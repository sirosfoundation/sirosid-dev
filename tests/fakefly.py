"""A stateful fake of the parts of `flyctl` that scripts/fly-up.py, fly-down.py
and scripts/fly_common.py use, plus a driver that runs the REAL fly-up.main()
against it with every other external effect (docker, sleeps, clocks, secret
generation, the post-deploy bootstrap call) pinned.

Two jobs:

1. Characterization. `run_fly_up()` returns the exact sequence of flyctl/docker
   commands a deploy issues and a content hash of every deterministic file it
   renders. tests/test_fly_up_characterization.py compares that with a golden,
   so a refactor of the deploy path can be proven to change nothing without a
   real Fly org. This is the safety net under the core-library extraction.
2. A fake backend for the future service's tests: it models apps, machines,
   volumes, secrets and tokens, so create/destroy/idempotent-redeploy behave
   like the real thing for the purposes of the orchestration code.

Not modelled: networking, real health, billing, anything Fly-side beyond what
the repo's own code parses. helm, openssl and bash (create-pki.sh) are NOT
faked - they run for real, which is why this needs helm on PATH.
"""
import hashlib
import importlib.util
import json
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent

# Files whose bytes differ run to run (fresh keys) and carry no information the
# characterization needs - their EXISTENCE is still recorded.
VOLATILE = re.compile(
    r"(^|/)(apiAuthKey\.pem|api_auth_jwks\.json|values\.api-auth\.yaml|vc-pki/.*|"
    r"vc-pki|.*\.pem|.*\.crt|.*\.key)$")


def _cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class FakeFly:
    """Just enough of flyctl. State is per instance, so a test can run the same
    deploy twice against one FakeFly to exercise idempotency."""

    def __init__(self, existing_apps=()):
        self.apps = {}          # name -> {"machines": [...], "volumes": [...], "secrets": {...}, "tokens": [...]}
        self.log = []           # normalized command strings, in order
        self._n = 0
        for a in existing_apps:
            self._app(a)

    def runner(self):
        """A subprocess.run-compatible callable for FlyClient(runner=...). Also
        records the FLY_API_TOKEN each call was made with, so tests can assert a
        client acts as the identity it was given."""
        self.tokens_seen = getattr(self, "tokens_seen", [])

        def run(cmd, **kw):
            self.tokens_seen.append((kw.get("env") or {}).get("FLY_API_TOKEN"))
            return self.handle(cmd[1:])
        return run

    def _app(self, name):
        return self.apps.setdefault(name, {"machines": [], "volumes": [], "secrets": {}, "tokens": []})

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n:04d}"

    @staticmethod
    def _opt(argv, *names, default=None):
        for n in names:
            if n in argv:
                i = argv.index(n)
                if i + 1 < len(argv):
                    return argv[i + 1]
        return default

    def handle(self, argv):
        argv = [str(a) for a in argv]
        self.log.append("flyctl " + " ".join(argv))
        app = self._opt(argv, "-a", "--app")
        head = argv[0] if argv else ""
        sub = argv[1] if len(argv) > 1 else ""

        if head == "apps" and sub == "list":
            return _cp(argv, stdout=json.dumps([{"Name": n} for n in self.apps]))
        if head == "apps" and sub == "create":
            self._app(argv[2])
            return _cp(argv)
        if head == "apps" and sub == "destroy":
            self.apps.pop(argv[2], None)
            return _cp(argv)
        if head == "ips":
            return _cp(argv)
        if head == "auth":
            return _cp(argv)
        if head == "secrets" and sub == "list":
            return _cp(argv, stdout=json.dumps([{"name": k} for k in self._app(app)["secrets"]]))
        if head == "secrets" and sub == "set":
            for kv in argv[2:]:
                if "=" in kv and not kv.startswith("-"):
                    k, v = kv.split("=", 1)
                    self._app(app)["secrets"][k] = v
            return _cp(argv)
        if head == "volumes" and sub == "list":
            return _cp(argv, stdout=json.dumps(self._app(app)["volumes"]))
        if head == "volumes" and sub == "create":
            vol = {"id": self._id("vol_"), "name": argv[2], "region": self._opt(argv, "-r", "--region", default="arn"),
                   "size_gb": int(self._opt(argv, "-s", "--size", default="1")), "state": "created"}
            self._app(app)["volumes"].append(vol)
            return _cp(argv, stdout=json.dumps(vol))
        if head in ("machine", "machines") and sub == "list":
            return _cp(argv, stdout=json.dumps(self._app(app)["machines"]))
        if head == "machine" and sub in ("start", "stop", "destroy"):
            ms = self._app(app)["machines"]
            for m in list(ms):
                if m["id"] == argv[2]:
                    if sub == "destroy":
                        ms.remove(m)
                    else:
                        m["state"] = "started" if sub == "start" else "stopped"
            return _cp(argv)
        if head == "checks" and sub == "list":
            return _cp(argv, stdout=json.dumps(
                {m["id"]: [{"name": "fake", "status": "passing"}] for m in self._app(app)["machines"]}))
        if head == "tokens" and sub == "create":
            tok = f"FlyV1 fake-token-{app}"
            self._app(app)["tokens"].append({"ID": self._id("tok_"), "Name": self._opt(argv, "--name", default="")})
            return _cp(argv, stdout=json.dumps({"token": tok}))
        if head == "tokens" and sub == "list":
            return _cp(argv, stdout=json.dumps(self._app(app)["tokens"]))
        if head == "tokens" and sub == "revoke":
            return _cp(argv)
        if head == "ssh":
            return _cp(argv, returncode=1)   # nothing readable from a fake machine
        if head == "deploy":
            self._deploy(app, argv)
            return _cp(argv)
        return _cp(argv, returncode=1, stderr=f"fake flyctl: unhandled {head} {sub}")

    def _deploy(self, app, argv):
        a = self._app(app)
        toml_path = self._opt(argv, "-c", "--config")
        mounts = []
        region = "arn"
        if toml_path and Path(toml_path).exists():
            text = Path(toml_path).read_text()
            m = re.search(r"\[mounts\][^\[]*?source\s*=\s*['\"]([^'\"]+)", text)
            if m:
                mounts = [{"name": m.group(1), "volume": "vol_fake"}]
            r = re.search(r"primary_region\s*=\s*['\"]([^'\"]+)", text)
            if r:
                region = r.group(1)
        image = self._opt(argv, "-i", "--image", default="")
        if a["machines"]:
            m = a["machines"][0]
            m.update(state="started", config={"image": image, "mounts": mounts})
        else:
            a["machines"].append({"id": self._id("mach_"), "state": "started", "region": region,
                                  "private_ip": f"fdaa::{len(self.apps):x}", "config": {"image": image, "mounts": mounts}})


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalize(text, extra=()):
    text = text.replace(str(ROOT), "<ROOT>")
    # Secret VALUES never belong in a golden (and fresh keys differ per run);
    # the name and the fact it was set are what matter.
    text = re.sub(r"(flyctl secrets set \S+?)=\S+", r"\1=<redacted>", text)
    for e in extra:
        text = text.replace(str(e), "<TMP>")
    return text


def hash_tree(out_dir: Path):
    files = {}
    for p in sorted(out_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(out_dir).as_posix()
            files[rel] = "(volatile)" if VOLATILE.search(rel) else hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return files


def run_fly_up(argv, fake=None, env_yaml=None, env="coretest", keep_state=False):
    """Run the real scripts/fly-up.py main() against `fake`.

    argv: fly-up arguments (without --env; it is added).  env_yaml: the text of
    an environments/<env>.yaml to use, or None for no file. State lands in
    fixtures/rendered/fly-<env> (gitignored) and is removed afterwards unless
    keep_state - never use a real environment's name here.
    """
    import os
    import unittest.mock as mock

    fake = fake or FakeFly()
    out_dir = ROOT / "fixtures" / "rendered" / f"fly-{env}"
    if out_dir.exists() and not keep_state:
        shutil.rmtree(out_dir)
    rng = random.Random(1234)
    real_run = subprocess.run
    real_which = shutil.which

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "flyctl":
            return fake.handle(cmd[1:])
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "docker":
            fake.log.append("docker " + " ".join(str(c) for c in cmd[1:]))
            return _cp(cmd, returncode=1 if cmd[1:3] == ["image", "inspect"] else 0)
        if "capture_output" not in kw and "stdout" not in kw:
            kw["stdout"] = subprocess.DEVNULL   # child scripts' chatter is not the test's business
            kw.setdefault("stderr", subprocess.DEVNULL)
        return real_run(cmd, *a, **kw)

    sys.path.insert(0, str(ROOT / "scripts"))
    envdir = None
    if env_yaml is not None:
        import tempfile
        envdir = Path(tempfile.mkdtemp(prefix="coretest-env-"))
        (envdir / "environments").mkdir()
        (envdir / "environments" / f"{env}.yaml").write_text(env_yaml)
    fly_up = _load("fly_up_under_test", ROOT / "scripts" / "fly-up.py")
    import env_config
    import android_apps
    import tempfile as _tf
    empty_root = Path(_tf.mkdtemp(prefix="coretest-empty-"))   # no .android-apps / .env.android of whoever runs this
    old_android_root = android_apps.SIROSID_DEV_ROOT
    old_root = env_config.SIROSID_DEV_ROOT
    old_cwd = os.getcwd()
    result = SimpleNamespace(exit=0)
    try:
        if envdir is not None:
            env_config.SIROSID_DEV_ROOT = envdir
        android_apps.SIROSID_DEV_ROOT = empty_root
        os.chdir(ROOT)
        with mock.patch.object(subprocess, "run", fake_run), \
             mock.patch("time.sleep", lambda *_: None), \
             mock.patch("time.time", lambda: 1_700_000_000.0), \
             mock.patch("secrets.choice", lambda seq: rng.choice(seq)), \
             mock.patch("shutil.which", lambda n, *a, **k: "/usr/bin/flyctl" if n == "flyctl" else real_which(n, *a, **k)), \
             mock.patch.object(fly_up.bootstrap, "register",
                               lambda *a, **k: {"issuer": "registered", "verifier": "registered"}), \
             mock.patch.object(sys, "argv", ["fly-up.py", "--env", env, "--region", "arn", *argv]):
            import contextlib
            import io
            sink = io.StringIO()
            try:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    fly_up.main()
            except SystemExit as e:
                result.exit = e.code if isinstance(e.code, int) else 1
                result.error = str(e.code)
    finally:
        os.chdir(old_cwd)
        env_config.SIROSID_DEV_ROOT = old_root
        android_apps.SIROSID_DEV_ROOT = old_android_root
        shutil.rmtree(empty_root, ignore_errors=True)
        if envdir is not None:
            shutil.rmtree(envdir, ignore_errors=True)
    extra = [envdir] if envdir else []
    result.commands = [_normalize(c, extra) for c in fake.log]
    result.files = hash_tree(out_dir) if out_dir.exists() else {}
    result.fake = fake
    if out_dir.exists() and not keep_state:
        shutil.rmtree(out_dir)
    return result
