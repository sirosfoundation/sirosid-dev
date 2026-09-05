#!/usr/bin/env python3
"""Storage lifecycle for a sirosid-dev environment, from outside it.

Two layers share the work of "clear the data":

  - env-admin (env-admin/server.py) runs INSIDE the environment and can stop
    the consumers, drop the databases, restart them and re-register the
    issuer/verifier. It is what the dashboard button calls. While an
    environment is up, this script just calls it too - one implementation.
  - When the environment is DOWN there is no env-admin, so this script falls
    back to the storage itself: the named Docker volumes locally, the Fly
    volumes (and the Mongo machines holding them) on Fly.

    make storage-status [ENV=<name>]       make storage-clear [ENV=<name>] [YES=yes]
    make fly-storage-clear ENV=<name>      python3 scripts/storage.py status --target fly --env <name>
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

LOCAL_VOLUMES = ["sirosid-mongodb-data", "sirosid-conformance-mongodb-data",
                 "sirosid-r2ps-softhsm-tokens", "sirosid-attest-softhsm-tokens"]
LOCAL_ENV_ADMIN = "http://localhost:3002"


# ---------------------------------------------------------------------------
# env-admin client (both targets)
# ---------------------------------------------------------------------------

class EnvAdmin:
    def __init__(self, base_url: str, token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)

    def status(self):
        try:
            code, body = self._req("GET", "/api/storage", timeout=5)
            return body if code == 200 else None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    def reset(self, env_name: str) -> str:
        try:
            code, body = self._req("POST", "/api/storage/reset", {"confirm": env_name})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit(f"env-admin rejected the reset (HTTP {e.code}): {detail}")
        return body["id"]

    def follow(self, job_id: str, out=print):
        """Tail the SSE stream until this job finishes."""
        req = urllib.request.Request(f"{self.base}/api/events")
        with urllib.request.urlopen(req, timeout=900) as resp:
            event = None
            for raw in resp:
                line = raw.decode(errors="replace").rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    data = json.loads(line[6:])
                    if data.get("id") != job_id:
                        continue
                    if event == "reset_step":
                        out(f"  [{data.get('level', 'info')}] {data['message']}")
                    elif event == "reset_finished":
                        return data["status"], data.get("error")
        return "unknown", "event stream ended"


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------

def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def local_volumes() -> list:
    out = docker("volume", "ls", "--format", "{{.Name}}", check=False).stdout.split()
    present = [v for v in LOCAL_VOLUMES if v in out]
    result = []
    for v in present:
        info = json.loads(docker("volume", "inspect", v).stdout)[0]
        users = docker("ps", "-a", "--filter", f"volume={v}", "--format", "{{.Names}} {{.State}}", check=False).stdout.split("\n")
        result.append({"name": v, "created": info.get("CreatedAt", "")[:19],
                       "containers": [u for u in users if u.strip()]})
    return result


def local_token() -> str:
    import os
    return os.environ.get("ENV_ADMIN_TOKEN") or os.environ.get("ADMIN_TOKEN", "")


def local_status(env_name: str):
    plan = stack.build_plan(env_name, with_checks=False)
    print("Stores this configuration gives the stack:")
    for s in plan.stores:
        print(f"  {s['name']:<20}{'persistent' if s['persistent'] else 'ephemeral':<11} {s['volume'] or s['kind']}"
              + (f"  ({s['note']})" if s.get("note") else ""))
    print("\nNamed volumes present on this machine:")
    vols = local_volumes()
    if not vols:
        print("  (none)")
    for v in vols:
        running = [c for c in v["containers"] if c.endswith(" running")]
        print(f"  {v['name']:<40} created {v['created']}  {'in use by ' + ', '.join(c.split()[0] for c in running) if running else 'not in use'}")
    st = EnvAdmin(LOCAL_ENV_ADMIN).status()
    print("\nenv-admin:", "not reachable (stack down?)" if not st else
          f"env={st['env']} reset_in_progress={st['reset_in_progress']}")
    if st and st.get("mongo", {}).get("reachable"):
        for d in st["mongo"]["databases"]:
            print(f"  {d['name']:<24}{d.get('documents', 0):>8} docs  {d.get('size_bytes', 0) / 1024:>10.1f} KB")


def confirm(prompt: str, yes: bool):
    if yes:
        return
    answer = input(f"{prompt} [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise SystemExit("aborted")


def clear_via_env_admin(admin: EnvAdmin, env_name: str, yes: bool) -> bool:
    st = admin.status()
    if not st:
        return False
    confirm(f"env-admin for '{st['env']}' is up. Wipe every database and restart its services?", yes)
    job = admin.reset(st["env"])
    print(f"reset {job} started - following progress:")
    status, error = admin.follow(job)
    if status != "finished":
        raise SystemExit(f"reset {status}: {error}")
    print("done - the environment is back to its freshly deployed state")
    return True


def local_clear(env_name: str, yes: bool):
    if clear_via_env_admin(EnvAdmin(LOCAL_ENV_ADMIN, local_token()), env_name, yes):
        return
    vols = local_volumes()
    if not vols:
        print("stack is down and no sirosid-* volumes exist - nothing to clear")
        return
    busy = [v for v in vols if any(c.endswith(" running") for c in v["containers"])]
    if busy:
        raise SystemExit("volumes are in use by running containers but env-admin is not reachable: "
                         + ", ".join(v["name"] for v in busy)
                         + "\n  either `make up` (so env-admin can do a clean reset) or `make down` first")
    print("stack is down - removing the named volumes: " + ", ".join(v["name"] for v in vols))
    confirm("Delete them? The next `make up` starts from empty data.", yes)
    # Stopped containers still reference the volumes; remove them first.
    for v in vols:
        for c in v["containers"]:
            docker("rm", "-f", c.split()[0], check=False)
        docker("volume", "rm", v["name"])
        print(f"  removed {v['name']}")


# ---------------------------------------------------------------------------
# Fly
# ---------------------------------------------------------------------------

def flyctl(*args, check=True):
    return subprocess.run(["flyctl", *args], capture_output=True, text=True, check=check)


def fly_env_admin(env_name: str) -> EnvAdmin:
    from fly_common import app_url
    token_path = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{env_name}" / "adminToken"
    token = token_path.read_text().strip() if token_path.exists() else ""
    return EnvAdmin(app_url(env_name, "wallet-frontend") + "/_admin", token)


def fly_volumes(env_name: str) -> list:
    from fly_common import STORAGE_APPS, app_name
    result = []
    for comp in STORAGE_APPS:
        app = app_name(env_name, comp)
        r = flyctl("volumes", "list", "-a", app, "--json", check=False)
        if r.returncode != 0:
            continue
        for v in json.loads(r.stdout or "[]"):
            result.append({"app": app, "id": v["id"], "name": v["name"], "region": v.get("region"),
                           "size_gb": v.get("size_gb"), "attached": bool(v.get("attached_machine_id"))})
    return result


def fly_status(env_name: str):
    admin = fly_env_admin(env_name)
    st = admin.status()
    print(f"env-admin ({admin.base}):", "not reachable (environment down?)" if not st else
          f"env={st['env']} reset_in_progress={st['reset_in_progress']}")
    if st and st.get("mongo", {}).get("reachable"):
        for d in st["mongo"]["databases"]:
            print(f"  {d['name']:<24}{d.get('documents', 0):>8} docs  {d.get('size_bytes', 0) / 1024:>10.1f} KB")
    print("\nFly volumes:")
    vols = fly_volumes(env_name)
    if not vols:
        print("  (none)")
    for v in vols:
        print(f"  {v['app']:<40} {v['name']:<28} {v['region']}  {v['size_gb']} GB  {'attached' if v['attached'] else 'detached'}")


def fly_clear(env_name: str, yes: bool, token: str = ""):
    admin = fly_env_admin(env_name)
    if token:
        admin.token = token
    if not admin.token:
        raise SystemExit(f"no admin token for '{env_name}' - fixtures/rendered/fly-{env_name}/adminToken is missing "
                         "(deployed from another machine?). Pass --token <adminToken>.")
    if clear_via_env_admin(admin, env_name, yes):
        return
    vols = fly_volumes(env_name)
    if not vols:
        print(f"environment '{env_name}' has no env-admin reachable and no volumes - nothing to clear")
        return
    print("env-admin is not reachable - the environment is down (or kept with KEEP_DATA). Volumes:")
    for v in vols:
        print(f"  {v['app']}: {v['name']} ({v['region']}, {v['size_gb']} GB)")
    confirm("Destroy these volumes (and the stopped Mongo machines holding them)? "
            "The next `make fly-up` recreates them empty.", yes)
    for v in vols:
        machines = json.loads(flyctl("machine", "list", "-a", v["app"], "--json", check=False).stdout or "[]")
        for m in machines:
            flyctl("machine", "destroy", m["id"], "-a", v["app"], "--force", check=False)
            print(f"  destroyed machine {m['id']} of {v['app']}")
        flyctl("volumes", "destroy", v["id"], "-a", v["app"], "--yes")
        print(f"  destroyed volume {v['name']} ({v['id']})")
    time.sleep(1)
    print(f"done - run `make fly-up ENV={env_name}` to bring the environment back with empty data")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["status", "clear"])
    parser.add_argument("--target", choices=["local", "fly"], default="local")
    parser.add_argument("--env", default="", help="environment name (required for --target fly)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--token", default="", help="--target fly: the environment's admin token, if not cached locally")
    args = parser.parse_args(argv)

    if args.target == "fly":
        if not args.env:
            raise SystemExit("--env <name> is required for --target fly")
        (fly_status if args.action == "status" else lambda e: fly_clear(e, args.yes, args.token))(args.env)
    else:
        (local_status if args.action == "status" else lambda e: local_clear(e, args.yes))(args.env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
