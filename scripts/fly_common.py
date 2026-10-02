"""Shared component registry + flyctl helpers for scripts/fly-up.py and fly-down.py.

See scripts/render-helm-config.py's module docstring for the overall Fly
design (one Fly app per component, `sirosid-<env>-<component>` naming,
images pulled straight from the siros-id-stack chart's values.yaml, no local
build). wallet-backend has no public Fly service - `wallet-proxy` (a small
nginx app mirroring fixtures/wallet-proxy.conf) is its public identity,
serving /.well-known/assetlinks.json for Android passkey verification and
proxying everything else through to wallet-backend, matching how local
Android/tunnel testing already works.
"""
import base64
import json
import urllib.error
import urllib.request
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core.assets import (  # noqa: E402,F401  (re-exported: the CLI scripts import these from here)
    assetlinks_json, merge_android_identities, mini_oidc_config, wallet_frontend_conf,
    wallet_frontend_dashboard_html, wallet_proxy_conf, write_fly_toml)
from sirosid_core.components import (  # noqa: E402,F401
    CONFORMANCE_COMPONENTS, FLY_REGION_FALLBACK, MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET,
    STORAGE_APPS, VOLUME_SIZE_GB, build_components, volume_name)
from sirosid_core.naming import Naming  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def _values_fly_image(key: str, default: str) -> str:
    """Read one pin from values-fly.yaml's images: block.

    Most components get their image through `helm template` with
    values-fly.yaml layered on top, so their pins live in that file. A few
    (mini-oidc, mongodb) aren't in the siros-id-stack chart at all and so
    can't ride that path - but their pins belong in the same file regardless,
    or they end up buried in a Python literal that nobody remembers to bump.
    Hence reading the key directly here rather than via helm.

    Falls back to `default` if the file or key is missing, so a checkout with
    a trimmed values-fly.yaml still deploys.
    """
    try:
        import yaml  # imported lazily: only fly-up needs it, not fly-down
        with open(SIROSID_DEV_ROOT / "values-fly.yaml") as fh:
            data = yaml.safe_load(fh) or {}
        return (data.get("images") or {}).get(key) or default
    except (OSError, ImportError, AttributeError):
        return default


MINI_OIDC_IMAGE = _values_fly_image(
    "miniOidc", "ghcr.io/sirosfoundation/mini-oidc:0.0.4"
)
# env-admin is sirosid-dev's own code (env-admin/), published to GHCR by
# .github/workflows/env-admin-image.yml. Not in the chart, so pinned here
# like mini-oidc. fly-up falls back to building it locally when the pin is
# not pullable yet (see fly-up.py's env-admin branch).
ENV_ADMIN_IMAGE = _values_fly_image(
    "envAdmin", "ghcr.io/sirosfoundation/sirosid-env-admin:0.1.0"
)
FLY_ORG = "sirosfoundation"

# The deployment order; the two pins above are the only repo-file input.
COMPONENTS = build_components(MINI_OIDC_IMAGE, ENV_ADMIN_IMAGE)



def detect_region() -> str:
    """Fly's own suggestion for where to run: the anycast edge nearest here.

    Every Fly API response carries `fly-request-id: <id>-<region>`, and the
    region is whichever edge anycast routed to - which is what `fly launch`
    means by the nearest region. One HEAD request, short timeout, and any
    failure falls back rather than blocking a deploy.

    Note this is genuinely per-machine: from Stockholm it answers 'fra', not
    'arn', so it is Fly's routing view rather than raw geography. That is the
    right thing to follow - it is the same view that decides where traffic to
    the deployed apps lands.
    """
    req = urllib.request.Request("https://api.fly.io/", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            request_id = resp.headers.get("fly-request-id", "")
    except urllib.error.HTTPError as e:
        # The bare path 404s, but the edge still stamps the header on its way
        # out - that response is just as good as a 200 for this.
        request_id = e.headers.get("fly-request-id", "")
    except Exception:
        return ""
    region = request_id.rsplit("-", 1)[-1] if "-" in request_id else ""
    # Shape check rather than a hardcoded region list, which would go stale:
    # every Fly region code is three lowercase letters.
    return region if len(region) == 3 and region.isalpha() and region.islower() else ""





def app_name(env: str, component: str) -> str:
    return Naming(env).app(component)


def app_url(env: str, component: str) -> str:
    return Naming(env).url(component)


def run_fly(*args, check=True, capture=False):
    cmd = ["flyctl"] + list(args)
    print("+ " + " ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        if capture:
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"flyctl {args[0]} failed (exit {result.returncode})")
    return result


def app_exists(name: str) -> bool:
    result = run_fly("apps", "list", "--json", check=False, capture=True)
    if result.returncode != 0:
        return False
    apps = json.loads(result.stdout or "[]")
    return any(a.get("Name") == name for a in apps)


def network_name(env: str) -> str:
    """A dedicated 6PN network per environment - apps in one org otherwise
    share ONE flat private network by default (any app can resolve/reach any
    other app's `.internal` address), which would mean any other developer's
    environment - or any other app in `sirosfoundation` - could reach this
    one's mongodb/pdp/wallet-backend directly. `--network` on `apps create`
    puts every component for this env in its own segment instead, so naming
    (`sirosid-<env>-*`) isn't the only thing preventing cross-environment
    reachability."""
    return Naming(env).network()


def ensure_app(name: str, network: str = None, allocate_public_ips: bool = False):
    """allocate_public_ips=True is only needed for a raw TCP-passthrough
    component (write_fly_toml's tcp_passthrough_port, e.g. "conformance") -
    confirmed empirically: unlike [http_service], a [[services]] block does
    NOT trigger Fly's usual automatic public IP allocation (`flyctl ips list`
    came back completely empty for such an app after a normal deploy, and its
    hostname didn't resolve anywhere, even though the machine itself was
    healthy). Both allocate-v4/v6 calls are idempotent (a no-op printing the
    existing IP if one's already allocated), so this is safe to call on
    every deploy, not just the first.
    """
    if app_exists(name):
        print(f"app {name} already exists")
    else:
        args = ["apps", "create", name, "-o", FLY_ORG, "--yes"]
        if network:
            args += ["--network", network]
        run_fly(*args)
    if allocate_public_ips:
        run_fly("ips", "allocate-v4", "--shared", "-a", name)
        run_fly("ips", "allocate-v6", "-a", name)


def is_local_docker_image(ref: str) -> bool:
    """True only for a bare image reference with no registry/namespace
    prefix (no '/') that's also present in the local Docker daemon - e.g.
    wallet-backend-e2e-test:local, exactly what `make up REBUILD=yes` /
    docker compose build already produce. The no-'/' check is deliberate:
    a real registry ref always has at least one ('ghcr.io/org/image', or
    even bare 'org/image'), so this never mistakes an intentionally-remote
    --images value for a local build just because it also happens to be
    cached locally (e.g. from a prior `docker pull` done only to inspect it).
    """
    if "/" in ref:
        return False
    try:
        result = subprocess.run(["docker", "image", "inspect", ref], capture_output=True)
    except FileNotFoundError:
        return False  # no local `docker` at all - fall through to a normal pull attempt
    return result.returncode == 0


_docker_authed_to_fly = False


def push_local_image(app: str, local_ref: str) -> str:
    """Tags and pushes a locally-built image (see is_local_docker_image) into
    `app`'s own registry.fly.io namespace and returns the pushed ref, so the
    caller can deploy it with a normal `-i <ref>` exactly like any other
    --images override - no manual `docker tag`/`docker push`/`flyctl auth
    docker` required from the developer. registry.fly.io namespaces images
    per Fly app, so `app` must already exist (ensure_app() always runs
    before this is called in deploy_component()) - Fly rejects a push
    against an app name it doesn't recognize. Tagged with the push time
    rather than reused as-is: repushing under a fixed tag would still work
    (Fly resolves the manifest fresh on every deploy, no client-side image
    cache involved), but a unique tag makes it obvious in the Fly dashboard
    which push a given deploy actually came from.
    """
    global _docker_authed_to_fly
    if not _docker_authed_to_fly:
        # One-time per run - reuses the developer's own `flyctl auth login`
        # session, no separate registry credential to manage.
        run_fly("auth", "docker")
        _docker_authed_to_fly = True
    remote_ref = f"registry.fly.io/{app}:local-{int(time.time())}"
    subprocess.run(["docker", "tag", local_ref, remote_ref], check=True)
    subprocess.run(["docker", "push", remote_ref], check=True)
    return remote_ref


def ensure_running(app: str):
    """`fly deploy` on a previously-stopped machine (e.g. crash-looped in an
    earlier attempt, or a service-less internal app with no autostart path
    at all) updates its config but doesn't necessarily start it - confirmed
    empirically (vc-issuer stayed 'stopped' after a config-only update
    following an earlier crash). Explicitly starts any machine still not
    running post-deploy, for every component, not just internal-only ones.
    """
    result = run_fly("machine", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return
    try:
        machines = json.loads(result.stdout or "[]")
    except ValueError:
        return
    for m in machines:
        # Only a machine that is actually at rest. Right after a deploy a
        # machine is briefly 'created'/'starting'/'replacing', and a start
        # request then fails with a failed_precondition that only looks alarming.
        if m.get("state") in ("stopped", "suspended"):
            run_fly("machine", "start", m["id"], "-a", app, check=False)


def machine_private_ip(app: str) -> str | None:
    """The 6PN IPv6 address of an app's (first) machine - queried via the
    Fly API, not DNS (no need to be on the WireGuard mesh to run this from a
    developer's own laptop, unlike an actual `.internal` lookup). Used for
    conformance-suite-nginx's hardcoded `proxy_pass http://server:8080` (no
    env var to retarget it - see CONFORMANCE_COMPONENTS): baking this literal
    IP into that app's own /etc/hosts as "server" lets its unmodified,
    published config resolve correctly without either forking the image or
    relying on a Docker-only `resolver 127.0.0.11` directive that doesn't
    exist on Fly's network.
    """
    result = run_fly("machine", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return None
    try:
        machines = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return machines[0]["private_ip"] if machines else None


def wait_for_checks(app: str, timeout: int = 90, poll_interval: int = 3):
    """Polls `flyctl checks list` until every check on `app` reports healthy,
    or gives up after `timeout` seconds (printing a warning, not failing the
    whole deploy - a slow-to-report check shouldn't block the rest of the
    environment when the machine itself did start).

    Generalizes what used to be a single `time.sleep(10)` after mongodb's
    deploy specifically (a pragmatic guess at how long Fly's 6PN DNS/routing
    takes to propagate for a brand-new machine) into an actual wait for a
    real signal, for every internal-only component that now has a machine
    check (write_fly_toml's `internal_check`) - not just mongodb.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = run_fly("checks", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode == 0:
            try:
                # {machine_id: [check, ...], ...} - NOT a flat list.
                by_machine = json.loads(result.stdout or "{}")
            except ValueError:
                by_machine = {}
            checks = [c for machine_checks in by_machine.values() for c in machine_checks]
            if checks and all(c.get("status") == "passing" for c in checks):
                print(f"{app}: all checks passing")
                return
        time.sleep(poll_interval)
    print(f"{app}: checks did not report passing within {timeout}s - continuing anyway "
          f"(check `flyctl checks list -a {app}` if the next component fails to reach it)",
          file=sys.stderr)


def destroy_app(name: str):
    if not app_exists(name):
        print(f"app {name} does not exist, skipping")
        return
    run_fly("apps", "destroy", name, "--yes")


def list_machines(app: str) -> list:
    result = run_fly("machine", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout or "[]")
    except ValueError:
        return []


def stop_machines(app: str):
    """For `fly-down --keep-data`: keep the app and its volume, stop paying
    for a running machine. `fly-up` starts it again (ensure_running)."""
    for m in list_machines(app):
        if m.get("state") not in ("stopped", "destroyed"):
            run_fly("machine", "stop", m["id"], "-a", app, check=False)


def list_volumes(app: str) -> list:
    result = run_fly("volumes", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout or "[]")
    except ValueError:
        return []


def ensure_volume(app: str, name: str, region: str, size_gb: int = VOLUME_SIZE_GB) -> dict:
    """Create `name` in `region` for `app` unless one already exists.

    A volume pins its machine to a region, so an existing volume in a
    DIFFERENT region than this run wants is a hard error rather than a
    silent second volume: Fly would place the machine with the new volume
    and the old data would sit orphaned, which is the one outcome persistent
    storage exists to prevent. Relocating an environment means clearing its
    data (make fly-storage-clear / fly-down without KEEP_DATA) first.
    """
    existing = [v for v in list_volumes(app) if v.get("name") == name and v.get("state") != "destroyed"]
    if existing:
        vol = existing[0]
        if region and vol.get("region") and vol["region"] != region:
            raise SystemExit(
                f"{app}: volume {name} ({vol['id']}) lives in region {vol['region']} but this run targets "
                f"{region}. A volume pins its machine, so the environment cannot move without losing the data.\n"
                f"  Either redeploy in {vol['region']} (REGION={vol['region']}, or pin region: in "
                f"environments/<name>.yaml), or clear the data first: make fly-storage-clear ENV=<name> "
                f"(or make fly-down ENV=<name> without KEEP_DATA) and deploy again.")
        print(f"{app}: volume {name} exists ({vol['id']}, {vol.get('region')}, {vol.get('size_gb')} GB)")
        return {**vol, "created": False}
    result = run_fly("volumes", "create", name, "-a", app, "-r", region or FLY_REGION_FALLBACK,
                     "-s", str(size_gb), "--yes", "--json", capture=True)
    try:
        vol = json.loads(result.stdout or "{}")
    except ValueError:
        vol = {}
    print(f"{app}: created volume {name} ({vol.get('id', '?')}, {region}, {size_gb} GB)")
    # `created` tells the caller the data is EMPTY - the one moment a Mongo
    # root password may (must, for an app that predates volumes and still
    # carries a rotated secret nobody knows) be set fresh.
    return {**vol, "created": True}


def machine_has_mount(machine: dict, volume: str) -> bool:
    """Whether a machine's config mounts the named volume (by volume name or
    id - the API reports the id, the fly.toml names the volume)."""
    for m in (machine.get("config") or {}).get("mounts") or []:
        if m.get("name") == volume or m.get("volume") == volume or str(m.get("volume", "")).startswith("vol_"):
            return True
    return False


def assert_volume_mounted(app: str, volume: str):
    """Fail the deploy if the app's machine did not come up with its volume.
    A mount that silently does not apply means the data is on the machine's
    ephemeral disk again - exactly the state persistent storage exists to end,
    and the next redeploy would erase it."""
    machines = list_machines(app)
    if not machines:
        raise SystemExit(f"{app}: no machine after deploy - cannot confirm volume {volume} is mounted")
    if not all(machine_has_mount(m, volume) for m in machines):
        raise SystemExit(
            f"{app}: machine came up WITHOUT the volume mount ({volume}). Its data would be ephemeral "
            f"again and lost on the next deploy. Check the generated {app.split('-')[-1]}.fly.toml has a "
            f"[mounts] block and `flyctl machine list -a {app} --json` shows config.mounts.")
    print(f"{app}: volume {volume} mounted")


def destroy_machines_without_mount(app: str):
    """A machine created before this component had a volume cannot have one
    attached after the fact - `fly deploy` refuses to add a mount to an
    existing machine. Its data was ephemeral anyway (that is the state this
    migration ends), so destroy it and let the deploy create a fresh one on
    the volume."""
    for m in list_machines(app):
        if not (m.get("config") or {}).get("mounts"):
            print(f"{app}: machine {m['id']} predates the volume (no mount) - replacing it")
            run_fly("machine", "destroy", m["id"], "-a", app, "--force", check=False)


def create_deploy_token(app: str, name: str = "sirosid-env-admin", expiry: str = "8760h") -> str:
    """An app-scoped deploy token: enough for the Machines API on THIS app
    (list/stop/start), nothing on any other app in the org. Created fresh on
    every fly-up (they are cheap and env-admin's secret is re-set with the
    new set), and revoked by fly-down via revoke_tokens()."""
    result = run_fly("tokens", "create", "deploy", "-a", app, "--name", name, "--expiry", expiry, "--json",
                     capture=True)
    try:
        data = json.loads(result.stdout or "{}")
        token = data.get("token") or ""
    except ValueError:
        token = ""
    if not token:
        # Older flyctl prints the bare token (sometimes prefixed "FlyV1 ").
        token = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if not token:
        raise SystemExit(f"could not create a deploy token for {app}: {result.stderr}")
    return token


def revoke_tokens(app: str, name: str = "sirosid-env-admin"):
    """Best effort: an app-scoped token is useless once the app is destroyed,
    so this is hygiene, not security-critical."""
    result = run_fly("tokens", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return
    try:
        tokens = json.loads(result.stdout or "[]")
    except ValueError:
        return
    for t in tokens:
        if t.get("Name", t.get("name")) == name:
            run_fly("tokens", "revoke", t.get("ID", t.get("id")), check=False)


def read_machine_file(app: str, path: str) -> str:
    """Read a file from the app's running machine over `fly ssh console`.

    Fly secrets cannot be read back through the API, but a `--file-secret`
    IS readable from inside the machine it is mounted into - which is how a
    developer who did not do the last deploy (and so has no local secret
    cache) can recover mongodb's root password instead of deploying a
    mismatched one. Empty string on any failure."""
    result = run_fly("ssh", "console", "-a", app, "-C", f"cat {path}", check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def existing_secret_names(app: str) -> set:
    result = run_fly("secrets", "list", "-a", app, "--json", check=False, capture=True)
    if result.returncode != 0:
        return set()
    try:
        return {s["name"] for s in json.loads(result.stdout or "[]")}
    except (ValueError, KeyError):
        return set()


def ensure_secret(app: str, key: str, value: str, force: bool = False):
    """Idempotent by default: never rotates a secret that's already set,
    mirroring render-helm-config.py's gen_secret() file-based idempotency.
    Staged (not applied immediately) - the `flyctl deploy` call that follows
    in fly-up.py picks up staged secrets on its own, so there's no running
    machine yet to redundantly restart here on a first-ever deploy.

    force=True skips the existing-value check entirely - only correct for a
    secret whose consumers ALL get redeployed together in the same run with
    the same freshly-generated value (e.g. mongodb's root password: mongo has
    no persistent volume, so every deploy starts from empty data anyway,
    meaning there's no old state a stale password would need to keep
    matching - unlike, say, the VC signing key, where an old private key
    deployed to only some consumers alongside new public certs on others
    would break credential verification).

    Every current caller mounts this secret into a file via `--file-secret`
    (not consumed as a plain env var) - and `--file-secret` base64-DECODES
    the stored secret value when writing the destination file (confirmed
    empirically: a plaintext value came out the other end as binary garbage;
    storing the base64 encoding of that same value produced the correct
    plaintext file). So the value handed to `flyctl secrets set` here must
    already be base64-encoded, or every `--file-secret`-mounted consumer
    silently gets a corrupted file - previously unnoticed for jwtSecret/
    adminToken only because they're opaque random tokens nothing else needs
    to match; it broke mongodb's root password and the VC signing key
    outright (auth failures / would-be signing failures) since those must
    equal a SPECIFIC value another component also holds.
    """
    if not force and key in existing_secret_names(app):
        print(f"secret {key} already set on {app}, leaving as-is")
        return
    encoded = base64.b64encode(value.encode()).decode()
    run_fly("secrets", "set", f"{key}={encoded}", "-a", app, "--stage")
















