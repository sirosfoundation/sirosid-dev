"""Everything the boot manager knows about sirosid-dev, with no UI in it.

The TUI (app.py) is a thin layer over this module, and this module is a thin
layer over the repo's own scripts: it imports scripts/stack.py for the option
matrix and plan, scripts/env_config.py for environments/<name>.yaml, and
scripts/storage.py's env-admin client for storage. Nothing about *how* the
stack is started lives here - every action is a `make` command, the same one
a developer would type, so the TUI can never do something `make` cannot.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


def find_repo_root() -> Path:
    """The sirosid-dev checkout this package was installed from (editable
    install: this file lives inside it), or the current directory if it looks
    like one."""
    here = Path(__file__).resolve()
    for candidate in [here.parents[2], Path.cwd(), *Path.cwd().parents]:
        if (candidate / "Makefile").is_file() and (candidate / "scripts" / "stack.py").is_file():
            return candidate
    raise SystemExit("could not find the sirosid-dev checkout (no Makefile + scripts/stack.py above here)")


ROOT = find_repo_root()
sys.path.insert(0, str(ROOT / "scripts"))

import stack  # noqa: E402
from env_config import config_path, load_environment_config  # noqa: E402
from storage import EnvAdmin, LOCAL_ENV_ADMIN, LOCAL_VOLUMES  # noqa: E402

FLY_APP_PREFIX = "sirosid-"
FLY_COMPONENT_MARKER = "-wallet-frontend"   # every environment has one; its presence = env exists


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

@dataclass
class Environment:
    name: str                       # "local" for the unnamed local stack, else the environments/<name>.yaml stem
    has_file: bool = False
    fly_deployed: bool = False
    region: str = ""
    local_options: dict = field(default_factory=dict)
    fly_summary: dict = field(default_factory=dict)   # images/trusted_* counts for the detail panel

    @property
    def file(self) -> Path:
        return config_path(self.name, ROOT)

    @property
    def env_arg(self) -> str:
        return "" if self.name == "local" else self.name


def list_environment_files() -> list[str]:
    d = ROOT / "environments"
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def fly_available() -> bool:
    return shutil.which("flyctl") is not None


def fly_environments() -> dict[str, list[str]]:
    """{env: [component apps]} for every sirosid-<env>-* app in the org.
    Empty when flyctl is missing or not logged in - the UI says so."""
    if not fly_available():
        return {}
    try:
        out = subprocess.run(["flyctl", "apps", "list", "--json"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    try:
        apps = json.loads(out.stdout or "[]")
    except ValueError:
        return {}
    envs: dict[str, list[str]] = {}
    names = [a.get("Name", "") for a in apps if a.get("Name", "").startswith(FLY_APP_PREFIX)]
    for name in names:
        if name.endswith(FLY_COMPONENT_MARKER):
            env = name[len(FLY_APP_PREFIX):-len(FLY_COMPONENT_MARKER)]
            envs[env] = [n for n in names if n.startswith(f"{FLY_APP_PREFIX}{env}-")]
    return envs


def load_environments(include_fly: bool = True) -> list[Environment]:
    envs: dict[str, Environment] = {"local": Environment("local")}
    for name in list_environment_files():
        cfg = load_environment_config(name, ROOT)
        e = Environment(name, has_file=True, region=cfg.get("region", ""))
        try:
            e.local_options = stack.options_from_env_file(name)
        except SystemExit as err:
            e.local_options = {"_error": str(err)}
        e.fly_summary = {
            "images": len(cfg.get("images", {})),
            "trusted_issuers": len(cfg.get("trusted_issuers", [])),
            "trusted_verifiers": len(cfg.get("trusted_verifiers", [])),
            "conformance": cfg.get("conformance", False),
            "chart_ref": cfg.get("chart_ref", ""),
        }
        envs[name] = e
    if include_fly:
        for name in fly_environments():
            envs.setdefault(name, Environment(name)).fly_deployed = True
    return list(envs.values())


# ---------------------------------------------------------------------------
# State probes
# ---------------------------------------------------------------------------

def docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def local_containers() -> dict[str, str]:
    """{container name: state} for the stack's compose containers."""
    try:
        out = subprocess.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project",
                              "--format", "{{.Names}}\t{{.State}}"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return {}
    result = {}
    for line in out.stdout.splitlines():
        if "\t" in line:
            name, state = line.split("\t", 1)
            if name.endswith("-e2e-test") or name.endswith("-e2e") or name.startswith("conformance"):
                result[name] = state
    return result


def local_state() -> str:
    containers = local_containers()
    if not containers:
        return "down"
    running = sum(1 for s in containers.values() if s == "running")
    if running == len(containers):
        return f"up ({running})"
    if running == 0:
        return f"stopped ({len(containers)})"
    return f"partial ({running}/{len(containers)})"


def local_volumes_present() -> list[str]:
    try:
        out = subprocess.run(["docker", "volume", "ls", "--format", "{{.Name}}"], capture_output=True, text=True,
                             timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    return [v for v in LOCAL_VOLUMES if v in out]


def health(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


LOCAL_HEALTH = [
    ("wallet-frontend", "http://localhost:3000/"),
    ("wallet-backend", "http://localhost:8080/health"),
    ("wallet-admin", "http://localhost:8081/admin/status"),
    ("wallet-engine", "http://localhost:8082/health"),
    ("env-admin", "http://localhost:3002/health"),
    ("go-trust", "http://localhost:9095/healthz"),
    ("go-trust (helm)", "http://localhost:9098/healthz"),
    ("vc-issuer", "http://localhost:9000/health"),
    ("vc-verifier", "http://localhost:9001/health"),
    ("vc-apigw", "http://localhost:9003/health"),
    ("vc-registry", "http://localhost:9004/health"),
    ("mini-oidc", "http://localhost:9005/.well-known/openid-configuration"),
]


def fly_health(env: str) -> list[tuple[str, bool]]:
    from fly_common import app_url
    base = app_url(env, "wallet-frontend")
    checks = ["backend", "admin", "engine", "registry", "pdp", "mini-oidc", "vc-registry", "vc-issuer",
              "vc-verifier", "vc-apigw", "env-admin"]
    return [("wallet-frontend", health(base + "/"))] + [(c, health(f"{base}/_health/{c}")) for c in checks]


# ---------------------------------------------------------------------------
# Plans and commands
# ---------------------------------------------------------------------------

def plan_for(env: Environment, overrides: dict | None = None) -> stack.Plan:
    return stack.build_plan(env.env_arg, overrides or {}, with_checks=True)


def make_cmd(target: str, env: Environment | None = None, extra: list[str] | None = None) -> list[str]:
    cmd = ["make", "--no-print-directory", target]
    if env and env.env_arg:
        cmd.append(f"ENV={env.env_arg}")
    cmd += extra or []
    return cmd


def up_cmd(env: Environment, overrides: dict | None = None) -> list[str]:
    """`make up ENV=<name> <flags>` - the file's local: block is applied by
    the Makefile itself; only per-run overrides are passed as flags."""
    extra = []
    for key, value in (overrides or {}).items():
        opt = stack.OPTION_BY_KEY[key]
        if opt["type"] == "bool":
            extra.append(f"{opt['make_var']}={'yes' if value else 'no'}")
        else:
            extra.append(f"{opt['make_var']}={value}")
    return make_cmd("up", env, extra)


def logs_cmd(env: Environment, fly: bool = False, component: str = "") -> list[str]:
    if fly:
        from fly_common import app_name
        return ["flyctl", "logs", "-a", app_name(env.name, component or "wallet-backend"), "--no-tail"]
    files = stack.build_plan(env.env_arg, {}, with_checks=False).compose_files
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += ["logs", "--tail", "200", "-f"]
    if component:
        cmd.append(component)
    return cmd


# ---------------------------------------------------------------------------
# environments/<name>.yaml: the `local:` block, edited without touching the rest
# ---------------------------------------------------------------------------

def write_local_block(name: str, options: dict) -> Path:
    """Replace (or append) the top-level `local:` block in environments/
    <name>.yaml by text, so the hand-written comments around every other key
    survive - a YAML round-trip would drop them."""
    path = config_path(name, ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else (
        f"# Persisted config for `make up ENV={name}` / `make fly-up ENV={name}` -\n"
        f"# see environments/README.md and scripts/env_config.py for the schema.\n")
    lines = []
    lines.append("local:")
    for opt in stack.OPTIONS:
        if opt.get("transient") or opt["key"] not in options:
            continue
        value = options[opt["key"]]
        if value == opt["default"]:
            continue
        if opt["type"] == "bool":
            lines.append(f"  {opt['key']}: {'true' if value else 'false'}")
        else:
            lines.append(f"  {opt['key']}: {json.dumps(str(value))}")
    if len(lines) == 1:
        lines = ["local: {}"]
    block = "\n".join(lines) + "\n"

    pattern = re.compile(r"^local:.*\n(?:^(?:[ \t]+.*|\s*)\n?)*", re.MULTILINE)
    m = pattern.search(text)
    if m:
        text = text[:m.start()] + block + text[m.end():]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text.strip() else "") + "# The local docker-compose stack's options for this environment -\n" \
                "# the same knobs `make up` takes on the command line (see `make plan`).\n" + block
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def storage_status(env: Environment, fly: bool) -> dict:
    """What the Storage screen shows. Combines the plan's view of the stores
    (what this configuration gives the stack) with env-admin's live view
    (sizes, counts) when the environment is up."""
    result = {"target": "fly" if fly else "local", "stores": [], "env_admin": None, "volumes": [], "token": ""}
    if fly:
        from storage import fly_env_admin, fly_volumes
        admin = fly_env_admin(env.name)
        result["token"] = admin.token
        result["env_admin"] = admin.status()
        result["admin_base"] = admin.base
        try:
            result["volumes"] = fly_volumes(env.name)
        except Exception as e:  # noqa: BLE001
            result["volumes_error"] = str(e)
    else:
        plan = stack.build_plan(env.env_arg, {}, with_checks=False)
        result["stores"] = plan.stores
        result["env_admin"] = EnvAdmin(LOCAL_ENV_ADMIN).status()
        result["admin_base"] = LOCAL_ENV_ADMIN
        result["volumes"] = [{"name": v} for v in local_volumes_present()]
        result["token"] = local_admin_token(plan.options.get("pdp", "allow"))
    return result


def local_admin_token(pdp: str) -> str:
    """The token wallet-backend accepts locally - mirrors the Makefile's
    _EFFECTIVE_ADMIN_TOKEN."""
    if pdp == "helm":
        p = ROOT / "fixtures" / "rendered-secrets" / "adminToken"
        if p.exists():
            return p.read_text().strip()
    return os.environ.get("ADMIN_TOKEN", "e2e-test-admin-token-for-testing-purposes-only")


def clear_storage(env: Environment, fly: bool, token: str, emit) -> tuple[str, str | None]:
    """Reset through env-admin, streaming step lines to `emit`. Returns
    (status, error). Only for an environment that is UP - the down case is
    `make storage-clear` / `make fly-storage-clear`, which the UI runs as a
    command instead."""
    if fly:
        from storage import fly_env_admin
        admin = fly_env_admin(env.name)
        if token:
            admin.token = token
    else:
        admin = EnvAdmin(LOCAL_ENV_ADMIN, token)
    st = admin.status()
    if not st:
        return "error", "env-admin is not reachable - is the environment up?"
    try:
        job = admin.reset(st["env"])
    except SystemExit as e:
        return "error", str(e)
    emit(f"reset {job} started")
    return admin.follow(job, out=emit)


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def _git(path: Path, *args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def doctor(env: Environment | None = None) -> list[dict]:
    """The gotchas from CLAUDE.md that can be checked mechanically, each
    with the fix next to it."""
    checks: list[dict] = []

    def add(name, ok, detail, fix=""):
        checks.append({"name": name, "ok": ok is None or bool(ok), "detail": detail, "fix": fix,
                       "skipped": ok is None})

    add("docker daemon", docker_ok(), "docker info answers", "start Docker / check permissions on the socket")
    for tool, why in (("helm", "renders every service's config"), ("flyctl", "Fly environments"),
                      ("cloudflared", "TUNNELS=yes"), ("python3", "the harness scripts")):
        add(f"{tool} on PATH", shutil.which(tool), why, f"install {tool}")

    for repo in ("wallet-frontend", "go-wallet-backend", "go-trust", "vc", "siros-id-stack"):
        path = ROOT.parent / repo
        if not path.is_dir():
            add(f"../{repo}", False, "missing", "make setup")
            continue
        branch = _git(path, "branch", "--show-current")
        dirty = bool(_git(path, "status", "--porcelain"))
        add(f"../{repo}", True, f"on {branch or 'detached'}{' (dirty)' if dirty else ''}")

    chart = ROOT.parent / "siros-id-stack"
    if chart.is_dir():
        branch = _git(chart, "branch", "--show-current")
        add("siros-id-stack on main", branch == "main",
            f"on {branch!r} - a stale/wrong chart renders wrong config for everything, not just the PDP",
            "git -C ../siros-id-stack checkout main && git -C ../siros-id-stack pull")
        behind = _git(chart, "rev-list", "--count", "HEAD..origin/main")
        add("siros-id-stack up to date", behind in ("", "0"), f"{behind or 0} commits behind origin/main",
            "git -C ../siros-id-stack pull --ff-only")

    # mini-oidc: the one local image pulled rather than built - check the
    # cached image is the pinned tag, not a stale floating one.
    pin = ""
    try:
        import yaml
        pin = ((yaml.safe_load((ROOT / "values-fly.yaml").read_text()) or {}).get("images") or {}).get("miniOidc", "")
    except Exception:  # noqa: BLE001
        pass
    if pin:
        try:
            r = subprocess.run(["docker", "image", "inspect", pin, "--format", "{{.Created}}"],
                               capture_output=True, text=True, timeout=10)
            add("mini-oidc image pinned", True, f"{pin} {'cached ' + r.stdout.strip()[:10] if r.returncode == 0 else 'not pulled yet (pulled on first VC=yes)'}")
        except (OSError, subprocess.SubprocessError):
            pass

    add("rendered secrets", (ROOT / "fixtures" / "rendered-secrets" / "adminToken").exists() or None,
        "PDP=helm's generated admin token exists (register-vc-services/env-admin use it in that mode)",
        "make up PDP=helm renders it")
    add("VC PKI", (ROOT / "fixtures" / "vc-pki" / "rootCA.crt").exists() or None,
        "fixtures/vc-pki generated", "make pki (make up VC=yes does it)")

    if env and env.env_arg and fly_available():
        from fly_common import app_name
        out_dir = ROOT / "fixtures" / "rendered" / f"fly-{env.name}"
        has_cache = (out_dir / "mongoRootPassword").exists()
        try:
            r = subprocess.run(["flyctl", "volumes", "list", "-a", app_name(env.name, "mongodb"), "--json"],
                               capture_output=True, text=True, timeout=30)
            vols = json.loads(r.stdout or "[]") if r.returncode == 0 else []
        except (OSError, subprocess.SubprocessError, ValueError):
            vols = []
        if vols:
            add(f"fly-{env.name}: Mongo password cached", has_cache,
                "the volume's root password is in fixtures/rendered/fly-<env>/ (fly-up recovers it over ssh otherwise)",
                f"make fly-up ENV={env.name} recovers it, or copy the directory from the deploying machine")
            region = vols[0].get("region")
            if env.region and region and env.region != region:
                add(f"fly-{env.name}: volume region", False,
                    f"volume in {region} but environments/{env.name}.yaml pins region: {env.region}",
                    "align region: or clear the data before moving")
    return checks
