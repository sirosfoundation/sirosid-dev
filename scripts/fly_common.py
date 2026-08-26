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
import subprocess
import sys
import time
from pathlib import Path

from android_apps import hex_to_apk_key_hash

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
FLY_ORG = "sirosfoundation"
FLY_REGION = "arn"

# mini-oidc's OIDC client registered for vc-apigw's auth_providers.oidc (PID/EHIC
# issuance) - a single source of truth for both sides of this pairing:
# mini_oidc_config() (below) sets these as the client mini-oidc itself knows
# about, and scripts/patch-vc-config-fly.py sets the SAME values explicitly in
# apigw's oidc config, instead of each independently hardcoding a literal that
# only works because it happens to match the other (previously: vc-config.yaml's
# base fixture hardcoded "apigw-oidc-client"/"test-secret", and this module's
# ${VAR:-default} fallbacks coincidentally matched it - nothing enforced that).
MINI_OIDC_APIGW_CLIENT_ID = "apigw-oidc-client"
MINI_OIDC_APIGW_CLIENT_SECRET = "test-secret"

# Deployment order matters - no `depends_on` equivalent on Fly, so components
# are deployed strictly in this order and each `fly deploy` blocks until its
# machine passes health checks before the next one starts.
COMPONENTS = [
    {
        "name": "mongodb",
        "image": "mongo:{mongo_version}",
        "ports": [{"internal": 27017, "public": False}],
        "checks": None,
        # No HTTP endpoint to check - a TCP check on mongod's own port, so
        # `deploy_component()` can wait for an actual accept-connections
        # signal instead of the fixed sleep() this replaces (see its comment
        # in fly-up.py's git history for the "connection refused" crash-loop
        # that motivated the original sleep in the first place).
        "internal_check": {"type": "tcp", "port": 27017},
    },
    {
        # Not part of siros-id-stack (sirosid-dev/testing-only, see
        # docker-compose.vc-services.yml) - a minimal OIDC Provider standing
        # in for a real government/eIDAS IdP behind vc-apigw's OIDC auth
        # provider (pid/pid_1_5/pid_1_8/ehic issuance). Must be public - the
        # end user's browser/app is redirected here to log in. Only the `op`
        # role is deployed; `mini-oidc-rp` is a separate test-harness client
        # for exercising the OP standalone, not part of the real apigw flow.
        "name": "mini-oidc",
        # Pinned to a real release tag, never the floating `:main`. The pin
        # itself lives in values-fly.yaml's images: block alongside every
        # other component's - mini-oidc isn't in the siros-id-stack chart, so
        # it can't be overlaid via `helm template` like the
        # image_from_helm_deployment components around it, and is read from
        # that file directly instead (see MINI_OIDC_IMAGE above).
        "image": MINI_OIDC_IMAGE,
        "ports": [{"internal": 9005, "public": True}],
        "checks": "/health",
    },
    {
        "name": "vc-registry",
        "image_from_helm_deployment": "issuer-registry",
        "ports": [{"internal": 8080, "public": True}],
        "checks": "/health",
    },
    {
        "name": "vc-issuer",
        "image_from_helm_deployment": "issuer-core",
        "ports": [{"internal": 8080, "public": False}, {"internal": 8090, "public": False}],
        "checks": None,
        # Internal-only (no [http_service]), so nothing previously blocked a
        # deploy on this actually becoming healthy before vc-verifier/vc-apigw
        # (which call it over 6PN) started deploying right after.
        "internal_check": {"type": "http", "port": 8080, "path": "/health"},
    },
    {
        "name": "vc-verifier",
        "image_from_helm_deployment": "verifier",
        "ports": [{"internal": 8080, "public": True}],
        "checks": "/health",
    },
    {
        "name": "vc-apigw",
        "image_from_helm_deployment": "issuer-apigw",
        "ports": [{"internal": 8080, "public": True}],
        "checks": "/health",
    },
    {
        "name": "pdp",
        "image_from_helm_deployment": "pdp",
        "ports": [{"internal": 8080, "public": False}],
        "checks": None,
        # Same reasoning as vc-issuer - wallet-backend calls pdp over 6PN
        # right after this, with nothing previously confirming it came up.
        "internal_check": {"type": "http", "port": 8080, "path": "/healthz"},
    },
    {
        "name": "wallet-backend",
        "image_from_helm_deployment": "wallet-backend",
        "ports": [{"internal": 8080, "public": False}, {"internal": 8081, "public": False}, {"internal": 8082, "public": False}],
        "checks": None,
        # Same reasoning - wallet-proxy (deployed right after) proxies to
        # this over 6PN with no prior confirmation it was actually healthy.
        "internal_check": {"type": "http", "port": 8080, "path": "/health"},
    },
    {
        "name": "wallet-proxy",
        "image": "nginx:alpine",
        "ports": [{"internal": 8090, "public": True}],
        "checks": "/.well-known/assetlinks.json",
    },
    {
        "name": "wallet-frontend",
        "image_from_helm_deployment": "wallet-frontend",
        "ports": [{"internal": 80, "public": True}],
        "checks": "/",
    },
]

# Opt-in (--conformance), deployed AFTER the 10 above, matching local dev's
# CONFORMANCE=yes overlay. conformance-runner drives sirosid-tests' own
# Playwright conformance specs (ghcr.io/sirosfoundation/conformance-runner,
# published from sirosid-tests/conformance-runner/) against this
# environment's public wallet-frontend/wallet-proxy URLs, relaying live
# progress to the dashboard - see wallet_frontend_dashboard_html()'s
# Conformance tab. vc-proxy (the local overlay's 4th component, a
# self-signed TLS front for otherwise-plain-HTTP vc-services) isn't needed
# here: every Fly-public vc-service already has real TLS via Fly's own
# *.fly.dev certs, so the conformance suite can test against
# vc-apigw/vc-verifier's public URLs directly - no proxy required.
CONFORMANCE_COMPONENTS = [
    {
        "name": "conformance-mongodb",
        # Pinned to the same fixed tag docker-compose.conformance.yml uses -
        # a requirement of this specific (older) conformance suite version,
        # not templated from siros-id-stack's mongoCommunityVersion like the
        # main mongodb component.
        "image": "mongo:6",
        "ports": [{"internal": 27017, "public": False}],
        "checks": None,
        "internal_check": {"type": "tcp", "port": 27017},
    },
    {
        "name": "conformance-server",
        "image": "registry.gitlab.com/openid/conformance-suite:latest",
        # Port 8080 confirmed via `docker inspect` (image's own
        # ExposedPorts/default BASE_URL=https://localhost:8443 env just
        # describes what it expects to be FRONTED by - the process itself
        # listens on plain 8080, TLS is entirely conformance-nginx's job).
        "ports": [{"internal": 8080, "public": False}],
        "checks": None,
        # Deliberately a plain TCP check, not HTTP against /api/runner/available
        # (matching the local Makefile's own readiness probe) - the app
        # itself REJECTS any request that doesn't carry X-Forwarded-Proto:
        # https (confirmed: "java.lang.RuntimeException: A non-https request
        # has been received by the conformance suite" in its logs), which a
        # bare Fly machine HTTP check can't set. TCP-open is a weaker signal
        # (doesn't confirm the Spring app finished initializing) but avoids
        # tripping that guard - wait_for_checks()'s timeout tolerance covers
        # the gap.
        "internal_check": {"type": "tcp", "port": 8080},
    },
    {
        "name": "conformance",
        "image": "registry.gitlab.com/openid/conformance-suite/nginx:latest",
        # No "public": True port here - this component is public via raw TCP
        # passthrough (write_fly_toml's tcp_passthrough_port), not
        # [http_service], since the image's baked-in nginx.conf hardcodes
        # `listen 8443 ssl` with its own self-signed cert (confirmed via
        # `docker run --entrypoint cat ... /etc/nginx/nginx.conf`) - Fly's
        # normal http_service forwards plain HTTP to internal_port, which
        # would fail the TLS handshake against an app that only speaks TLS.
        "ports": [{"internal": 8443, "public": True}],
        "checks": None,
    },
    {
        "name": "conformance-runner",
        "image": "ghcr.io/sirosfoundation/conformance-runner:main",
        "ports": [{"internal": 3001, "public": False}],
        "checks": None,
        # Internal-only - reached solely via wallet-frontend's own
        # /_conformance/ proxy over 6PN (see wallet_frontend_conf()), never
        # a direct public URL, same as pdp/vc-issuer. /health is a plain
        # unconditional 200 (deliberately NOT /api/status, which depends on
        # the conformance suite itself being reachable - the container's own
        # liveness shouldn't be coupled to that).
        "internal_check": {"type": "http", "port": 3001, "path": "/health"},
    },
]


def app_name(env: str, component: str) -> str:
    return f"sirosid-{env}-{component}"


def app_url(env: str, component: str) -> str:
    return f"https://{app_name(env, component)}.fly.dev"


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
    return f"sirosid-{env}"


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
        if m.get("state") != "started":
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


def write_fly_toml(path: Path, app: str, primary_public_port: int | None, process_cmd: str | None = None,
                    health_check_path: str | None = None, memory_mb: int = 256, internal_check: dict | None = None,
                    tcp_passthrough_port: int | None = None):
    """Minimal per-app fly.toml - image/files/secrets are passed as `fly deploy`
    flags (see fly-up.py), not baked in here. Only the app-level shape
    (region, autostart/autostop, the one public port if any, and a command
    override where the image's own CMD isn't already correct - e.g. go-trust's
    Dockerfile CMD is ["serve"], which its flag-based CLI treats as a bare
    positional and then stops parsing, silently ignoring any flags after it -
    `--config` must be passed with no "serve" ahead of it) lives in the file.

    Deliberately NOT using wmp-inspector's scale-to-zero
    (auto_stop_machines/min_machines_running=0) pattern: Fly's traffic-
    triggered autostart only fires for requests through the public edge
    proxy, never for direct 6PN internal calls between sibling apps (e.g.
    vc-apigw/vc-issuer calling vc-registry over gRPC) - confirmed by vc-registry
    going idle-stopped and then refusing internal connections indefinitely,
    with no way for its callers to wake it. `make fly-up`/`fly-down` (the
    whole environment's lifecycle) is this deployment's actual on-demand
    mechanism instead - every component here just stays running once up.
    """
    lines = [
        f"app = '{app}'",
        f"primary_region = '{FLY_REGION}'",
        "",
    ]
    if process_cmd is not None:
        lines += [
            "[processes]",
            f"  app = '{process_cmd}'",
            "",
        ]
    if tcp_passthrough_port is not None:
        # Raw TCP passthrough (no Fly-terminated TLS) - for an image that
        # insists on speaking TLS itself with its OWN (self-signed) cert,
        # e.g. the OpenID conformance suite's published nginx image (baked-in
        # `listen 8443 ssl` with a self-signed cert) - Fly's normal
        # [http_service] forwards plain HTTP to internal_port, which would
        # fail the TLS handshake against an app that only ever speaks TLS.
        # Passthrough means the browser sees the self-signed cert directly
        # (a warning to click through) instead of Fly's own valid cert -
        # matches local dev's own conformance-suite UX exactly (same
        # self-signed cert there too), not a regression.
        lines += [
            "[[services]]",
            f"  internal_port = {tcp_passthrough_port}",
            "  protocol = 'tcp'",
            "",
            "  [[services.ports]]",
            "    port = 443",
            "    handlers = []",
            "",
            "  [[services.tcp_checks]]",
            "    interval = '10s'",
            "    timeout = '5s'",
            "    grace_period = '15s'",
            "",
        ]
    if primary_public_port is not None:
        lines += [
            "[http_service]",
            f"  internal_port = {primary_public_port}",
            "  force_https = true",
            "  auto_stop_machines = 'off'",
            "  auto_start_machines = true",
            "  min_machines_running = 1",
        ]
        if process_cmd is not None:
            # Fly requires [http_service] to explicitly name which process
            # group it serves whenever [processes] is also defined (silent
            # default association stops applying) - "invalid app
            # configuration: Service has no processes set but app has 1
            # processes defined". Only surfaced once a component needed both
            # a command override AND a public service (mini-oidc - the
            # process_cmd-using components before it were all internal-only).
            lines += ["  processes = ['app']"]
        lines += [""]
        if health_check_path:
            lines += [
                "  [[http_service.checks]]",
                "    interval = '10s'",
                "    timeout = '5s'",
                "    grace_period = '15s'",
                "    method = 'GET'",
                f"    path = '{health_check_path}'",
                "",
            ]
    if internal_check is not None:
        # Machine-level check (distinct from [[http_service.checks]] above) -
        # works for apps with NO [http_service] at all, so an internal-only
        # component (vc-issuer, pdp, wallet-backend) can still have
        # deploy_component()'s post-deploy wait_for_checks() confirm it's
        # actually healthy before the next component - which calls it over
        # 6PN - starts deploying right after.
        lines += [
            "[checks]",
            "  [checks.internal]",
            f"    port = {internal_check['port']}",
            f"    type = '{internal_check['type']}'",
            "    interval = '10s'",
            "    timeout = '5s'",
            "    grace_period = '15s'",
        ]
        if internal_check["type"] == "http":
            lines += [
                "    method = 'GET'",
                f"    path = '{internal_check['path']}'",
            ]
        lines += [""]
    if memory_mb != 256:
        lines += [
            "[[vm]]",
            "  cpu_kind = 'shared'",
            "  cpus = 1",
            f"  memory_mb = {memory_mb}",
            "",
        ]
    path.write_text("\n".join(lines))


def mini_oidc_config(env: str) -> str:
    """mini-oidc's configs/config.production.yaml, baked into its image at
    /etc/mini-oidc/configs/config.production.yaml, with `ehic` added to
    scopes_supported (missing upstream - apigw's data_sources.assertion maps
    ehic to auth_provider: oidc, same as pid/pid_1_5/pid_1_8, but mini-oidc's
    own default scope list omits it). ${VAR} placeholders are expanded by
    mini-oidc's own binary from its container env at startup (see fly-up.py's
    env vars for this component) - this is the file's real content verbatim,
    not a Python-side template.
    """
    return """# Production / Docker configuration.
# Environment variables are expanded in string values: ${VAR_NAME}
server:
  op_port: 9005
  rp_port: 9006
  issuer: "${ISSUER}"
  scopes_supported:
    - openid
    - profile
    - email
    - organisation
    - pid
    - pid_1_5
    - pid_1_8
    - ehic

clients:
  - client_id: "${CLIENT_ID:-mini-oidc-rp}"
    client_name: "Relying Party"
    redirect_uris:
      - "${RP_BASE_URL}/callback"
    token_endpoint_auth_method: "none"

  - client_id: "${APIGW_CLIENT_ID}"
    client_name: "VC API Gateway"
    client_secret: "${APIGW_CLIENT_SECRET}"
    redirect_uris:
      - "${APIGW_REDIRECT_URI:-http://localhost:8091/oidcrp/callback}"
    token_endpoint_auth_method: "client_secret_basic"

rp:
  base_url: "${RP_BASE_URL}"
  client_id: "${CLIENT_ID:-mini-oidc-rp}"
  op_issuer: "${ISSUER}"
"""


def wallet_proxy_conf(env: str) -> str:
    """Fly-hostname variant of fixtures/wallet-proxy.conf's first server block
    (assetlinks.json + proxy to wallet-backend) - the second block (Android
    issuer proxy via vc-proxy) is conformance-suite-only, not part of this
    deployment.

    Does NOT serve apple-app-site-association (an earlier version of this
    function did) - iOS checks Associated Domains / passkey webcredentials at
    the RP ID's own domain, which is wallet-frontend's domain (see
    patch_wallet_backend_fly's rp_id), not wallet-proxy's, so a copy served
    here would be at the wrong domain to ever be consulted. wallet-frontend's
    own image already generates a complete AASA (both applinks AND
    webcredentials sections - see wallet-frontend/config/files/well-known.ts)
    from the same WELLKNOWN_APPLE_APPIDS env var fly-up.py already sets on it
    (_wallet_frontend_env), served correctly at its own domain automatically -
    confirmed live (GET https://sirosid-<env>-wallet-frontend.fly.dev/.well-
    known/apple-app-site-association returns the real file, not a 404 or the
    SPA fallback, once WELLKNOWN_APPLE_APPIDS is actually set on the deploy).

    Proxies the /admin/tenants/* subtree (tenant create/read/delete plus
    issuer/verifier registration for ANY tenant id, not just "default") -
    wallet-backend's admin port (8081) is otherwise 6PN-internal only and
    unreachable from outside the environment's network. Widened from an
    earlier version that exact-matched only /admin/tenants/default/issuers|
    verifiers (all `register_vc_services()` needed) once CDP-based WebAuthn
    conformance testing (sirosid-tests' tenant-setup-fixture.ts) needed to
    create/delete its OWN per-test tenants (POST /admin/tenants, GET/DELETE
    /admin/tenants/:id) rather than reusing the fixed "default" one. Still
    deliberately NOT a blanket `/admin/` proxy: wallet-backend's admin API
    also covers user/instance management, which has no reason to be
    reachable from the public internet even bearer-token-gated - only
    /admin/tenants and its immediate id/issuers/verifiers children match;
    everything else under /admin/ stays unreachable through wallet-proxy.
    """
    backend = f"{app_name(env, 'wallet-backend')}.internal"
    return f"""server {{
    listen 8090;
    # Fly's 6PN inter-app network is IPv6-only - without this, this app was
    # only ever reachable via Fly's public edge (which terminates externally
    # and forwards regardless), never via another app's *.internal hostname.
    # Confirmed live: wallet-frontend's own same-origin API proxy (added for
    # the AS session cookie's SameSite=Strict requirement - see
    # wallet_frontend_conf()) got a bare TCP connection reset dialing
    # wallet-proxy.internal:8090 until this was added.
    listen [::]:8090;

    # Matches go-wallet-backend's own MaxBodySize (pkg/middleware/bodysize.go)
    # - the private-data blob (S.credentials[] in the encrypted container)
    # grows unbounded as credentials accumulate, and mdoc/mDL credentials
    # each embed a base64 portrait photo. nginx's compiled-in default of 1m
    # was tight enough to 413 a real device after only a few mdoc
    # batch-issuance rounds against the same test account.
    client_max_body_size 10m;

    location /.well-known/assetlinks.json {{
        alias /etc/nginx/well-known/assetlinks.json;
        default_type application/json;
    }}

    location = /admin/tenants {{
        proxy_pass http://{backend}:8081;
        proxy_set_header Host $host;
    }}

    location ~ ^/admin/tenants/[^/]+$ {{
        proxy_pass http://{backend}:8081;
        proxy_set_header Host $host;
    }}

    location ~ ^/admin/tenants/[^/]+/(issuers|verifiers)$ {{
        proxy_pass http://{backend}:8081;
        proxy_set_header Host $host;
    }}

    # Individual issuer/verifier resource (PUT to update client_id/client_jwk,
    # DELETE) - the collection-only match above doesn't cover these since it's
    # an exact ($) match, not a prefix.
    location ~ ^/admin/tenants/[^/]+/(issuers|verifiers)/[^/]+$ {{
        proxy_pass http://{backend}:8081;
        proxy_set_header Host $host;
    }}

    location /api/v2/wallet {{
        proxy_pass http://{backend}:8082;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}

    location / {{
        proxy_pass http://{backend}:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


def wallet_frontend_conf(env: str, conformance: bool = False) -> str:
    """nginx config for wallet-frontend's own image on Fly (mirrors
    sirosid-dev's local nginx-e2e.conf: same dashboard-at-/, same asset
    prefix-stripping - see wallet_frontend_dashboard_html() for what differs
    about the dashboard itself).

    Without the prefix-stripping location, the image's OWN generated
    default.conf (root + `try_files $uri /index.html` with no location for
    the BASE_PATH prefix) can't find ANY file under /id/default/ - every
    request, including the JS bundle itself, silently falls through to
    index.html (confirmed: nginx access log showed 200 for
    /id/default/assets/index-*.js at the exact byte size of index.html) -
    the browser tries to execute HTML as a JavaScript module and the app
    never mounts, a blank page with no error surfaced anywhere server-side.

    Bare / must return a real 200 (not a redirect) - Fly's own health check
    hits GET / and expects 2xx; a 302 to /id/default/ fails it and Fly's
    edge stops routing to the machine entirely ("no known healthy instances
    found"). Serving the dashboard directly at / (matching local exactly)
    satisfies the health check AND gives the same landing page as local dev.

    conformance=True adds one more health-check proxy (conformance-server) -
    IMPORTANT ordering requirement: every one of these proxy_pass targets is
    a static (non-variable) hostname, which nginx resolves ONCE at config
    load/startup, not per-request - if the target doesn't exist/resolve yet,
    nginx refuses to start AT ALL (not just that one location), taking down
    the whole app including the dashboard and the actual wallet SPA. This is
    why every target here is always deployed before wallet-frontend in
    fly-up.py's sequence - conformance-server included, which is why
    main() interleaves CONFORMANCE_COMPONENTS around wallet-frontend instead
    of simply appending them at the very end.
    """
    backend = f"{app_name(env, 'wallet-backend')}.internal"
    wallet_proxy = f"{app_name(env, 'wallet-proxy')}.internal"
    pdp = f"{app_name(env, 'pdp')}.internal"
    mini_oidc = f"{app_name(env, 'mini-oidc')}.internal"
    vc_registry = f"{app_name(env, 'vc-registry')}.internal"
    vc_issuer = f"{app_name(env, 'vc-issuer')}.internal"
    vc_verifier = f"{app_name(env, 'vc-verifier')}.internal"
    vc_apigw = f"{app_name(env, 'vc-apigw')}.internal"
    conformance_server = f"{app_name(env, 'conformance-server')}.internal"
    conformance_runner = f"{app_name(env, 'conformance-runner')}.internal"
    conformance_health = (
        f"    location = /_health/conformance-server {{ proxy_pass "
        f"http://{conformance_server}:8080/api/runner/available; "
        # conformance-server rejects any request without this header
        # ("A non-https request has been received by the conformance
        # suite") - true here in spirit: this hop only happens because the
        # BROWSER already reached wallet-frontend over real https, exactly
        # what conformance-nginx's own X-Forwarded-Proto (set for real
        # external traffic) is meant to convey.
        f"proxy_set_header X-Forwarded-Proto https; "
        f"proxy_connect_timeout 2s; proxy_read_timeout 2s; }}\n"
        if conformance else ""
    )
    conformance_proxy = (
        # Mirrors nginx-e2e.conf's local /_conformance/ block exactly, so
        # startup.html's JS (ported verbatim into
        # wallet_frontend_dashboard_html()) needs zero changes - same-origin
        # same path, same prefix-stripping rewrite, same SSE-safe buffering
        # settings (proxy_buffering off / long proxy_read_timeout - this is
        # a long-lived text/event-stream connection, not a normal request).
        f"    location /_conformance/ {{\n"
        f"        proxy_pass http://{conformance_runner}:3001/;\n"
        f"        proxy_connect_timeout 5s;\n"
        f"        proxy_read_timeout 300s;\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Connection '';\n"
        f"        proxy_buffering off;\n"
        f"        proxy_cache off;\n"
        f"        chunked_transfer_encoding off;\n"
        f"    }}\n"
        if conformance else ""
    )
    return f"""server {{
    listen 80;
    absolute_redirect off;

    root /usr/share/nginx/html;

    # Dashboard landing page (see wallet_frontend_dashboard_html()) - a real
    # 200, not a redirect (see docstring above for why that matters).
    location = / {{
        root /usr/share/nginx;
        try_files /startup.html =404;
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'" always;
        add_header Cache-Control "no-store, no-cache, must-revalidate" always;
    }}

    # Health-check proxies for the dashboard above - same-origin fetch()
    # calls from the BROWSER hit these (not wallet-frontend's own process),
    # which then proxy server-side to each Fly app's 6PN .internal address -
    # reachable because every component in this environment shares one
    # --network (see fly_common.network_name), unlike the browser itself,
    # which can only ever reach *.fly.dev public URLs.
    location = /_health/backend  {{ proxy_pass http://{backend}:8080/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/admin    {{ proxy_pass http://{backend}:8081/admin/status; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/engine   {{ proxy_pass http://{backend}:8082/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/registry {{ proxy_pass http://{backend}:8080/registry/status; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/pdp        {{ proxy_pass http://{pdp}:8080/healthz; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/mini-oidc  {{ proxy_pass http://{mini_oidc}:9005/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-registry {{ proxy_pass http://{vc_registry}:8080/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-issuer   {{ proxy_pass http://{vc_issuer}:8080/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-verifier {{ proxy_pass http://{vc_verifier}:8080/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-apigw    {{ proxy_pass http://{vc_apigw}:8080/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
{conformance_health}
{conformance_proxy}
    # Same-origin proxy for wallet-frontend's own API calls (AuthServerClient,
    # AuthZENClient, private-data sync, etc. - everything under BACKEND_URL,
    # see fly-up.py's _wallet_frontend_env setting WALLET_BACKEND_URL to THIS
    # app's own URL rather than wallet-proxy's directly).
    #
    # Required for the AS session cookie, not just a nice-to-have: it's set
    # SameSite=Strict (internal/as/cookie.go), on the documented assumption
    # that "login/register are same-origin API calls" - true in a
    # single-domain production deployment, but wallet-frontend and
    # wallet-proxy are separate *.fly.dev subdomains, which are genuinely
    # cross-SITE (different registrable domains under the public suffix
    # list), not merely cross-origin. A SameSite=Strict cookie can never be
    # sent cross-site regardless of CORS/withCredentials settings, so every
    # session-cookie-dependent call silently 401ed ("authentication
    # required") without this - confirmed live, including
    # OpenID4VCIHelper.getAuthorizationServerMetadata's anonymous-token
    # bootstrap, which is why credential-issuance flows against a PAR-only
    # issuer never even attempted PAR.
    #
    # Proxies to wallet-proxy's own internal address (not wallet-backend's
    # directly) to reuse its existing admin-subtree/websocket-upgrade
    # routing (fly_common.wallet_proxy_conf) rather than duplicating it here.
    location ~ ^/(api|auth|v1|user|helper|issuer|oidc|presentation|storage|verifier|wallet-provider)/ {{
        proxy_pass http://{wallet_proxy}:8090;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}

    # Serve assets and static files from any /id/<tenant>/ prefix by
    # stripping the prefix and serving from the root - BASE_PATH's generated
    # index.html references everything as /id/default/assets/... but the
    # config-gen step writes files directly under the docroot, not nested
    # under a matching id/default/ subdirectory.
    location ~ ^/id/[^/]+/(.+\\.(js|css|svg|png|ico|json|woff2?|ttf|webmanifest|map|webm))$ {{
        try_files /$1 =404;
    }}

    # SPA fallback: every other /id/<tenant>/* route serves index.html.
    location / {{
        try_files $uri /index.html;
    }}
}}
"""


def wallet_frontend_dashboard_html(env: str, android_identities: dict[str, list[str]] | None = None,
                                    apple_app_ids: list[str] | None = None,
                                    conformance_url: str | None = None) -> str:
    """Fly-adapted version of sirosid-dev's local startup.html landing page
    (same navbar/branding/Quick-Links/Services-table styling, reusing its
    CSS near-verbatim), served at wallet-frontend's own bare / (see
    wallet_frontend_conf()).

    What's dropped from the local version, and why: the local Conformance
    Suite tab (SSE-driven test-runner UI + log viewer) talks to a
    "conformance-runner" container that's a documented placeholder with no
    real API (see conformance-runner/Dockerfile) - there was never anything
    working to port. The real, functional piece (the OpenID conformance
    suite itself) IS deployed when conformance_url is given (--conformance -
    see CONFORMANCE_COMPONENTS) - shown as a plain link, since driving test
    runs happens through the suite's own web UI either way, locally or on
    Fly. Build Info (git metadata from locally-built images) is dropped too -
    nothing to show for a Fly environment pulling published images. The
    Services table stays, retargeted at this environment's actual deployed
    services (see SERVICES below) - mock-trust-pdp/mock-verifier are dropped
    (not deployed on Fly; pdp/vc-verifier are the real equivalents), mini-oidc/
    vc-registry are added (deployed on Fly, no local equivalent in the
    original list).

    What's ADDED beyond the local version: an Environment Info card (backend/
    API URL, WebAuthn RP ID, tenant ID) and a Native App Setup card (every
    android:apk-key-hash: identity and iOS app ID this environment's
    wallet-backend actually accepts - android_identities is
    merge_android_identities()'s output, so it's guaranteed to match
    rp_origins exactly, not a second hand-maintained list that could drift)
    - local dev doesn't need either (a developer already knows their own
    localhost URLs and debug keystore), but a Fly environment's whole point
    is often to hand to someone else/a native app that doesn't already know
    any of this.
    """
    backend_url = app_url(env, "wallet-proxy")
    frontend_url = app_url(env, "wallet-frontend")
    rp_id = f"{app_name(env, 'wallet-frontend')}.fly.dev"
    android_rows = "".join(
        f'<tr><td class="svc-name">{package}</td>'
        f'<td class="meta"><code>android:apk-key-hash:{hex_to_apk_key_hash(fp)}</code></td></tr>'
        for package, fingerprints in (android_identities or {}).items()
        for fp in fingerprints
    )
    apple_rows = "".join(
        f'<tr><td class="svc-name">{app_id}</td></tr>'
        for app_id in (apple_app_ids or [])
    )
    service_list = [
        ("wallet-backend", "backend", 8080),
        ("wallet-admin", "admin", 8081),
        ("wallet-engine", "engine", 8082),
        ("vctm-registry", "registry", 8080),
        ("pdp (go-trust)", "pdp", 8080),
        ("mini-oidc", "mini-oidc", 9005),
        ("vc-registry", "vc-registry", 8080),
        ("vc-issuer", "vc-issuer", 8080),
        ("vc-verifier", "vc-verifier", 8080),
        ("vc-apigw", "vc-apigw", 8080),
    ]
    if conformance_url:
        service_list.append(("conformance-server", "conformance-server", 8080))
    services_js = ",\n  ".join(
        f'{{ name: "{name}", check: "/_health/{check}", port: {port} }}'
        for name, check, port in service_list
        if check
    )

    # Everything below is plain text (not an f-string) - ported near-verbatim
    # from startup.html, so no {{/}} brace-escaping is needed; it's spliced
    # into the outer f-string by reference below. Gated on conformance_url:
    # without --conformance there's no conformance-runner to talk to, so the
    # whole tab (and its CSS/JS) is simply omitted rather than shown dead,
    # unlike local dev's startup.html, which always shows it (and always did,
    # even when it was dead - see this function's docstring).
    conformance_css = """
  /* Conformance card styles */
  .conf-btn {
    display: inline-block;
    padding: 0.4rem 1rem;
    border: none;
    border-radius: 6px;
    color: #fff;
    font-weight: 500;
    font-size: 0.85rem;
    cursor: pointer;
    transition: background 0.15s, opacity 0.15s;
  }
  .conf-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .conf-btn-primary { background: #1C4587; }
  .conf-btn-primary:hover:not(:disabled) { background: #163a70; }
  .conf-btn-secondary { background: #555; }
  .conf-btn-secondary:hover:not(:disabled) { background: #444; }
  .conf-run-grid { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .conf-status { margin-bottom: 0.75rem; font-size: 0.9rem; }
  .conf-status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .conf-results { margin-top: 0.75rem; }
  .conf-results table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  .conf-results th { text-align: left; color: #555; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; padding: 0.35rem 0.5rem; border-bottom: 1px solid #e0e0e0; background: #f8f9fa; }
  .conf-results td { padding: 0.35rem 0.5rem; border-bottom: 1px solid #e0e0e0; }
  .conf-results td a { color: #1C4587; text-decoration: none; font-size: 0.75rem; }
  .conf-results td a:hover { text-decoration: underline; }
  .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
  .badge-pass { background: #dcfce7; color: #166534; }
  .badge-fail { background: #fee2e2; color: #991b1b; }
  .badge-warn { background: #fef9c3; color: #854d0e; }
  .badge-run  { background: #dbeafe; color: #1e40af; }
  .badge-skip { background: #f0f0f0; color: #555; }
  .run-header { cursor: pointer; padding: 0.6rem 0; border-bottom: 1px solid #e0e0e0; user-select: none; }
  .run-header:hover { background: #f8f9fa; }
  .run-toggle { display: inline-block; width: 1em; font-size: 0.8rem; color: #888; margin-right: 0.3rem; }
  .run-time { font-size: 0.75rem; color: #888; margin-left: 0.5rem; }
  .run-body { padding: 0.5rem 0; }
  .run-body.collapsed { display: none; }
  .run-summary { display: inline-flex; gap: 0.5rem; align-items: center; }
  /* Log viewer panel */
  .log-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.3); z-index: 200; }
  .log-overlay.active { display: block; }
  .log-panel {
    position: fixed; top: 0; right: 0; bottom: 0; width: min(75vw, 900px);
    background: #fff; box-shadow: -4px 0 20px rgba(0,0,0,0.15); z-index: 201;
    display: flex; flex-direction: column; transform: translateX(100%);
    transition: transform 0.2s ease;
  }
  .log-panel.active { transform: translateX(0); }
  .log-panel-header {
    display: flex; align-items: center; gap: 0.75rem; padding: 1rem 1.25rem;
    border-bottom: 1px solid #e0e0e0; background: #f8f9fa; flex-shrink: 0;
  }
  .log-panel-header h3 { font-size: 0.95rem; font-weight: 600; color: #1a1a1a; flex: 1; margin: 0; }
  .log-panel-close {
    background: none; border: none; font-size: 1.4rem; cursor: pointer;
    color: #888; padding: 0 0.3rem; line-height: 1;
  }
  .log-panel-close:hover { color: #333; }
  .log-panel-body { flex: 1; overflow-y: auto; padding: 1rem 1.25rem; }
  .log-entry { margin-bottom: 0.75rem; border: 1px solid #e8e8e8; border-radius: 6px; overflow: hidden; }
  .log-entry-header {
    display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.75rem;
    background: #f8f9fa; cursor: pointer; font-size: 0.82rem; user-select: none;
  }
  .log-entry-header:hover { background: #f0f0f0; }
  .log-src { font-weight: 600; color: #555; min-width: 5em; }
  .log-msg { flex: 1; color: #1a1a1a; word-break: break-word; }
  .log-entry-body { display: none; padding: 0.5rem 0.75rem; font-size: 0.78rem; background: #fafafa; border-top: 1px solid #e8e8e8; }
  .log-entry-body.open { display: block; }
  .log-entry-body pre {
    white-space: pre-wrap; word-break: break-all; margin: 0;
    font-family: 'SF Mono', 'Consolas', 'Monaco', monospace; font-size: 0.78rem;
    color: #333; max-height: 400px; overflow-y: auto;
  }
  .log-entry.log-fail { border-left: 3px solid #ef4444; }
  .log-entry.log-pass { border-left: 3px solid #22c55e; }
  .log-entry.log-warn { border-left: 3px solid #f59e0b; }
  .log-entry.log-info { border-left: 3px solid #3b82f6; }
  .log-module-info { margin-bottom: 1rem; padding: 0.75rem; background: #f8f9fa; border-radius: 6px; font-size: 0.85rem; }
  .log-module-info dt { font-weight: 600; color: #555; display: inline; }
  .log-module-info dd { display: inline; margin: 0 1rem 0 0.3rem; }
  /* Tabs */
  .tabs { display: flex; gap: 0; border-bottom: 2px solid #e0e0e0; margin-bottom: 1.25rem; }
  .tab {
    padding: 0.6rem 1.5rem;
    font-size: 0.9rem;
    font-weight: 600;
    color: #555;
    cursor: pointer;
    border: none;
    background: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color 0.15s, border-color 0.15s;
  }
  .tab:hover { color: #1C4587; }
  .tab.active { color: #1C4587; border-bottom-color: #1C4587; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
""" if conformance_url else ""

    tabs_bar = """
    <div class="tabs">
      <button class="tab active" onclick="switchTab('status')">Status</button>
      <button class="tab" onclick="switchTab('conformance')">Conformance</button>
    </div>""" if conformance_url else ""
    status_panel_open = '    <div id="tab-status" class="tab-panel active">' if conformance_url else ""
    status_panel_close = "    </div>" if conformance_url else ""

    conformance_tab = """
    <div id="tab-conformance" class="tab-panel">
      <div class="card" id="conformance-card">
        <h2>Conformance Suite</h2>
        <div class="conf-status" id="conf-status">
          <span class="conf-status-dot dot-checking"></span>Checking conformance suite&hellip;
        </div>
        <div class="conf-run-grid" id="conf-buttons"></div>
        <div class="conf-results" id="conf-results"></div>
      </div>
    </div>""" if conformance_url else ""

    conformance_log_viewer_html = """
<div id="log-overlay" class="log-overlay" onclick="closeLogPanel()"></div>
<div id="log-panel" class="log-panel" onclick="event.stopPropagation()">
  <div class="log-panel-header">
    <h3 id="log-panel-title">Log</h3>
    <button class="log-panel-close" onclick="closeLogPanel()" title="Close">&times;</button>
  </div>
  <div class="log-panel-body" id="log-panel-body"></div>
</div>""" if conformance_url else ""

    conformance_js = """
function switchTab(name) {
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelector('.tab[onclick*="' + name + '"]').classList.add('active');
}

function esc(s) {
  var d = document.createElement("div");
  d.appendChild(document.createTextNode(s));
  return d.innerHTML;
}

// =========================================================================
// Conformance Dashboard
// =========================================================================

var confBase = "/_conformance";
var confAvailable = false;
var confRuns = {};       // runId -> run state
var confEventSource = null;

function confStatusEl() { return document.getElementById("conf-status"); }

function checkConformance() {
  fetch(confBase + "/api/status")
    .then(function(r) { return r.json(); })
    .then(function(data) {
      confAvailable = data.conformance_suite === "connected";
      var dot = confAvailable ? "dot-up" : "dot-down";
      var label = confAvailable ? "Connected" : "Not available";
      confStatusEl().innerHTML =
        '<span class="conf-status-dot ' + dot + '"></span>' + label +
        ' <span class="meta">(' + esc(data.url || "") + ')</span>';
      if (confAvailable) {
        loadConfPlans();
        if (!confEventSource) connectSSE();
      }
    })
    .catch(function() {
      confAvailable = false;
      confStatusEl().innerHTML =
        '<span class="conf-status-dot dot-down"></span>Runner not available';
    });
}

function loadConfPlans() {
  fetch(confBase + "/api/plans")
    .then(function(r) { return r.json(); })
    .then(function(plans) {
      var el = document.getElementById("conf-buttons");
      el.innerHTML = "";
      plans.forEach(function(p) {
        var btn = document.createElement("button");
        btn.className = "conf-btn " + (p.phase === 1 ? "conf-btn-primary" : "conf-btn-secondary");
        btn.textContent = p.label;
        btn.title = p.planName + " (Phase " + p.phase + ")";
        btn.onclick = function() { startConfRun(p.id, btn); };
        el.appendChild(btn);
      });
    })
    .catch(function() {});
}

function startConfRun(planType, btn) {
  if (btn) btn.disabled = true;
  fetch(confBase + "/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ planType: planType })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        alert("Error: " + data.error);
        if (btn) btn.disabled = false;
        return;
      }
      confRuns[data.id] = {
        id: data.id,
        planType: data.planType,
        status: "creating",
        startedAt: Date.now(),
        modules: [],
        results: []
      };
      renderConfResults();
      setTimeout(function() { if (btn) btn.disabled = false; }, 5000);
    })
    .catch(function(err) {
      alert("Failed to start run: " + err);
      if (btn) btn.disabled = false;
    });
}

function connectSSE() {
  if (confEventSource) confEventSource.close();
  confEventSource = new EventSource(confBase + "/api/events");

  confEventSource.addEventListener("run_start", function(e) {
    var d = JSON.parse(e.data);
    confRuns[d.id] = confRuns[d.id] || { id: d.id, planType: d.planType, status: "creating", startedAt: Date.now(), modules: [], results: [] };
    confRuns[d.id].label = d.label;
    collapsedRuns[d.id] = false; // new runs start expanded
    renderConfResults();
  });

  confEventSource.addEventListener("run_update", function(e) {
    var d = JSON.parse(e.data);
    if (confRuns[d.id]) {
      confRuns[d.id].status = d.status;
      if (d.modules) confRuns[d.id].modules = d.modules;
      if (d.planId) confRuns[d.id].planId = d.planId;
      if (d.planDetailUrl) confRuns[d.id].planDetailUrl = d.planDetailUrl;
    }
    renderConfResults();
  });

  confEventSource.addEventListener("run_state", function(e) {
    var d = JSON.parse(e.data);
    confRuns[d.id] = d;
    renderConfResults();
  });

  confEventSource.addEventListener("module_event", function(e) {
    var d = JSON.parse(e.data);
    var run = confRuns[d.runId];
    if (!run) return;
    if (d.type === "module_start") {
      run.currentModule = d.module;
    } else if (d.type === "module_result") {
      run.results = run.results || [];
      var idx = run.results.findIndex(function(r) { return r.module === d.module; });
      var entry = { module: d.module, status: d.status, result: d.result, moduleId: d.moduleId };
      if (idx >= 0) run.results[idx] = entry;
      else run.results.push(entry);
      run.currentModule = null;
    }
    renderConfResults();
  });

  confEventSource.addEventListener("run_finished", function(e) {
    var d = JSON.parse(e.data);
    if (confRuns[d.id]) {
      confRuns[d.id].status = "finished";
      confRuns[d.id].passed = d.passed;
      confRuns[d.id].failed = d.failed;
      confRuns[d.id].total = d.total;
      confRuns[d.id].finishedAt = Date.now();
      confRuns[d.id].planDetailUrl = d.planDetailUrl;
      // Auto-expand finished runs
      collapsedRuns[d.id] = false;
    }
    renderConfResults();
  });

  confEventSource.addEventListener("run_error", function(e) {
    var d = JSON.parse(e.data);
    if (confRuns[d.id]) {
      confRuns[d.id].status = "error";
      confRuns[d.id].error = d.error;
    }
    renderConfResults();
  });

  confEventSource.onerror = function() {
    setTimeout(function() { if (confAvailable) connectSSE(); }, 5000);
  };
}

function resultBadge(result) {
  if (!result) return '<span class="badge badge-run">running</span>';
  var cls = result === "PASSED" ? "badge-pass"
          : result === "WARNING" ? "badge-warn"
          : result === "SKIPPED" || result === "REVIEW" ? "badge-skip"
          : "badge-fail";
  return '<span class="badge ' + cls + '">' + esc(result) + '</span>';
}

function formatTime(ts) {
  if (!ts) return "";
  var d = new Date(ts);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDuration(start, end) {
  if (!start || !end) return "";
  var secs = Math.round((end - start) / 1000);
  if (secs < 60) return secs + "s";
  return Math.floor(secs / 60) + "m " + (secs % 60) + "s";
}

var collapsedRuns = {};

function toggleRunCollapse(id) {
  collapsedRuns[id] = !collapsedRuns[id];
  var body = document.getElementById("run-body-" + id);
  if (body) body.classList.toggle("collapsed", !!collapsedRuns[id]);
  var toggle = document.getElementById("run-toggle-" + id);
  if (toggle) toggle.textContent = collapsedRuns[id] ? "▶" : "▼";
}

function renderConfResults() {
  var el = document.getElementById("conf-results");
  var ids = Object.keys(confRuns).sort(function(a, b) {
    return (confRuns[b].startedAt || 0) - (confRuns[a].startedAt || 0);
  });
  if (ids.length === 0) {
    el.innerHTML = '<span class="meta">No runs yet. Click a button above to start a conformance test plan.</span>';
    return;
  }
  var html = "";
  ids.forEach(function(id) {
    var run = confRuns[id];
    var isActive = run.status === "running" || run.status === "creating";
    if (!(id in collapsedRuns)) collapsedRuns[id] = !isActive && run.status !== "finished";
    var collapsed = collapsedRuns[id];

    var statusBadge = "";
    if (run.status === "finished") {
      var passCount = (run.results || []).filter(function(r) { return r.result === "PASSED"; }).length;
      var failCount = (run.results || []).filter(function(r) { return r.result !== "PASSED" && r.result !== "SKIPPED"; }).length;
      var total = (run.results || []).length;
      if (failCount === 0) {
        statusBadge = '<span class="badge badge-pass">' + passCount + '/' + total + ' passed</span>';
      } else {
        statusBadge = '<span class="badge badge-fail">' + failCount + ' failed</span> <span class="badge badge-pass">' + passCount + ' passed</span>';
      }
    } else if (run.status === "error") {
      statusBadge = '<span class="badge badge-fail">error</span>';
    } else if (run.status === "running") {
      statusBadge = '<span class="badge badge-run">running</span>';
    } else {
      statusBadge = '<span class="badge badge-run">' + esc(run.status) + '</span>';
    }

    var timeInfo = formatTime(run.startedAt);
    if (run.finishedAt) timeInfo += " (" + formatDuration(run.startedAt, run.finishedAt) + ")";

    html += '<div style="margin-bottom:0.5rem">';
    html += '<div class="run-header" onclick="toggleRunCollapse(\\'' + esc(id) + '\\')">';
    html += '<span class="run-toggle" id="run-toggle-' + esc(id) + '">' + (collapsed ? '▶' : '▼') + '</span>';
    html += '<strong>' + esc(run.label || run.planType) + '</strong> ';
    html += '<span class="run-summary">' + statusBadge + '</span>';
    if (timeInfo) html += '<span class="run-time">' + esc(timeInfo) + '</span>';
    if (run.planDetailUrl) {
      html += ' <a href="' + esc(run.planDetailUrl) + '" target="_blank" style="font-size:0.75rem;color:#1C4587" onclick="event.stopPropagation()">↗ suite</a>';
    }
    html += '</div>';

    html += '<div class="run-body' + (collapsed ? ' collapsed' : '') + '" id="run-body-' + esc(id) + '">';

    if (run.status === "error") {
      html += '<div style="color:#991b1b;font-size:0.85rem;padding:0.4rem 0">Error: ' + esc(run.error || "unknown") + '</div>';
    }

    var modules = run.modules || [];
    var results = run.results || [];
    if (modules.length > 0 || results.length > 0) {
      html += '<table><thead><tr><th>Module</th><th>Result</th><th></th></tr></thead><tbody>';
      results.forEach(function(r) {
        html += '<tr><td class="svc-name">' + esc(r.module) + '</td><td>' + resultBadge(r.result) + '</td>';
        html += '<td>';
        if (r.moduleId) {
          html += '<a href="#" onclick="event.preventDefault();showLog(\\'' + esc(r.moduleId) + '\\',\\'' + esc(r.module) + '\\')">view log</a>';
        }
        html += '</td></tr>';
      });
      var completedModules = results.map(function(r) { return r.module; });
      modules.forEach(function(m) {
        if (completedModules.indexOf(m) >= 0) return;
        var badge = m === run.currentModule
          ? '<span class="badge badge-run">running</span>'
          : '<span class="meta">pending</span>';
        html += '<tr><td class="svc-name">' + esc(m) + '</td><td>' + badge + '</td><td></td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';
    html += '</div>';
  });
  el.innerHTML = html;
}

function loadConfRuns() {
  fetch(confBase + "/api/runs")
    .then(function(r) { return r.json(); })
    .then(function(list) {
      list.forEach(function(run) { confRuns[run.id] = run; });
      renderConfResults();
    })
    .catch(function() {});
}

checkConformance();
loadConfRuns();
setInterval(checkConformance, 30000);

// =========================================================================
// Log Viewer
// =========================================================================

function showLog(moduleId, moduleName) {
  var overlay = document.getElementById("log-overlay");
  var panel = document.getElementById("log-panel");
  var body = document.getElementById("log-panel-body");
  var title = document.getElementById("log-panel-title");

  title.textContent = moduleName || moduleId;
  body.innerHTML = '<span class="meta">Loading log&hellip;</span>';
  overlay.classList.add("active");
  requestAnimationFrame(function() { panel.classList.add("active"); });

  Promise.all([
    fetch(confBase + "/api/info/" + encodeURIComponent(moduleId)).then(function(r) { return r.json(); }),
    fetch(confBase + "/api/log/" + encodeURIComponent(moduleId)).then(function(r) { return r.json(); })
  ]).then(function(results) {
    var info = results[0];
    var log = results[1];
    renderLogPanel(info, log);
  }).catch(function(err) {
    body.innerHTML = '<div style="color:#991b1b">Failed to load log: ' + esc(String(err)) + '</div>';
  });
}

function closeLogPanel() {
  var panel = document.getElementById("log-panel");
  var overlay = document.getElementById("log-overlay");
  panel.classList.remove("active");
  setTimeout(function() { overlay.classList.remove("active"); }, 200);
}

function renderLogPanel(info, log) {
  var body = document.getElementById("log-panel-body");
  var html = "";

  html += '<div class="log-module-info"><dl style="margin:0">';
  html += '<dt>Status:</dt><dd>' + resultBadge(info.result || info.status) + '</dd>';
  if (info.testModule) { html += '<dt>Module:</dt><dd>' + esc(info.testModule) + '</dd>'; }
  if (info.testName) { html += '<dt>Test:</dt><dd>' + esc(info.testName) + '</dd>'; }
  if (info.description) { html += '<dt>Description:</dt><dd>' + esc(info.description) + '</dd>'; }
  html += '</dl></div>';

  if (!Array.isArray(log) || log.length === 0) {
    html += '<span class="meta">No log entries.</span>';
  } else {
    log.forEach(function(entry, idx) {
      var src = entry.src || "";
      var msg = entry.msg || "";
      var result = entry.result || "";
      var entryClass = "log-info";
      if (result === "FAILURE") entryClass = "log-fail";
      else if (result === "WARNING") entryClass = "log-warn";
      else if (result === "SUCCESS") entryClass = "log-pass";
      else if (msg.toLowerCase().indexOf("error") >= 0 || msg.toLowerCase().indexOf("fail") >= 0) entryClass = "log-fail";

      html += '<div class="log-entry ' + entryClass + '">';
      html += '<div class="log-entry-header" onclick="toggleLogEntry(' + idx + ')">';
      if (result) html += resultBadge(result === "FAILURE" ? "FAILED" : result) + ' ';
      html += '<span class="log-src">' + esc(src) + '</span>';
      html += '<span class="log-msg">' + esc(msg.substring(0, 200)) + (msg.length > 200 ? "..." : "") + '</span>';
      html += '</div>';

      html += '<div class="log-entry-body" id="log-entry-' + idx + '">';
      if (msg.length > 200) {
        html += '<p><strong>Message:</strong></p><pre>' + esc(msg) + '</pre>';
      }
      var skipKeys = {"src":1, "msg":1, "result":1, "_type":1};
      var details = Object.keys(entry).filter(function(k) { return !skipKeys[k] && entry[k]; });
      if (details.length > 0) {
        details.forEach(function(k) {
          var val = entry[k];
          if (typeof val === "object") val = JSON.stringify(val, null, 2);
          else val = String(val);
          html += '<p style="margin:0.3rem 0"><strong>' + esc(k) + ':</strong></p>';
          html += '<pre>' + esc(val) + '</pre>';
        });
      }
      html += '</div>';
      html += '</div>';
    });
  }
  body.innerHTML = html;
}

function toggleLogEntry(idx) {
  var el = document.getElementById("log-entry-" + idx);
  if (el) el.classList.toggle("open");
}
""" if conformance_url else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sirosid-{env}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Helvetica Neue', Arial, system-ui, sans-serif;
    min-height: 100vh; color: #1a1a1a; background: #fff;
    font-size: 14px; line-height: 1.5;
  }}
  .navbar {{ background: #fff; border-bottom: 1px solid #e0e0e0; position: sticky; top: 0; z-index: 100; }}
  .navbar-inner {{ max-width: 1200px; margin: 0 auto; padding: 0.6rem 2rem; display: flex; align-items: center; }}
  .navbar-brand {{ display: flex; align-items: center; gap: 0.6rem; text-decoration: none; color: #1C4587; font-weight: 600; font-size: 1.1rem; }}
  .navbar-brand svg {{ height: 32px; width: auto; }}
  .navbar-links {{ margin-left: auto; display: flex; gap: 1.25rem; align-items: center; }}
  .navbar-links a {{ color: #555; text-decoration: none; font-size: 0.875rem; font-weight: 500; transition: color 0.2s; }}
  .navbar-links a:hover {{ color: #1C4587; }}
  .content {{ max-width: 1200px; margin: 2rem auto; padding: 0 2rem; }}
  h1 {{ color: #1C4587; font-size: 1.6rem; margin-bottom: 0.5rem; font-weight: 700; }}
  .subtitle {{ color: #555; margin-bottom: 1.5rem; font-size: 0.95rem; }}
  a {{ color: #1C4587; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1.25rem; margin-bottom: 1.25rem; }}
  .card h2 {{ font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.03em; color: #555; font-weight: 600; margin-bottom: 0.75rem; }}
  .links {{ display: flex; gap: 0.75rem; flex-wrap: wrap; }}
  .links a {{ display: inline-block; padding: 0.5rem 1.25rem; background: #1C4587; border: none; border-radius: 6px; color: #fff; font-weight: 500; font-size: 0.9rem; transition: background 0.15s; }}
  .links a:hover {{ background: #163a70; text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; color: #555; font-weight: 600; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; padding: 0.5rem 0.75rem; border-bottom: 1px solid #e0e0e0; background: #f8f9fa; }}
  td {{ padding: 0.5rem 0.75rem; border-bottom: 1px solid #e0e0e0; vertical-align: top; }}
  .svc-name {{ font-weight: 500; color: #1a1a1a; white-space: nowrap; }}
  .status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
  .dot-up {{ background: #22c55e; }}
  .dot-down {{ background: #ccc; }}
  .dot-checking {{ background: #f59e0b; animation: pulse 1s ease-in-out infinite; }}
  @keyframes pulse {{ 50% {{ opacity: 0.4; }} }}
  .meta {{ color: #555; font-size: 0.8rem; }}
  .footer {{ border-top: 1px solid #e0e0e0; margin-top: 2rem; padding: 1.5rem 0; font-size: 0.8rem; color: #555; }}
  .footer-inner {{ max-width: 1200px; margin: 0 auto; padding: 0 2rem; display: flex; align-items: center; justify-content: space-between; }}
  .footer a {{ color: #555; transition: color 0.2s; }}
  .footer a:hover {{ color: #1C4587; text-decoration: none; }}
  .port {{ color: #888; font-size: 0.75rem; font-weight: normal; }}
{conformance_css}</style>
</head>
<body>
  <nav class="navbar">
    <div class="navbar-inner">
      <a class="navbar-brand" href="/">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 239 234" width="32" height="32">
          <path fill="#1C4587" d="M 51.746094 89.585938 C 85.816406 84.324219 85.816406 84.324219 91.074219 50.246094 C 96.335938 84.324219 96.335938 84.324219 130.40625 89.585938 C 96.328125 94.84375 96.335938 94.910156 91.074219 128.929688 C 85.816406 94.847656 85.816406 94.847656 51.746094 89.585938 M 162.640625 217.410156 C 153.421875 157.6875 153.421875 157.6875 93.714844 148.46875 C 153.421875 139.246094 153.421875 139.246094 162.640625 79.523438 C 171.621094 137.710938 171.863281 139.207031 227.089844 147.773438 C 229.964844 137.921875 231.511719 127.503906 231.511719 116.71875 C 231.511719 55.589844 181.964844 6.03125 120.847656 6.03125 C 59.734375 6.03125 10.1875 55.589844 10.1875 116.71875 C 10.1875 177.851562 59.734375 227.410156 120.847656 227.410156 C 170.65625 227.410156 212.777344 194.496094 226.660156 149.226562 C 171.84375 157.730469 171.597656 159.472656 162.640625 217.410156"/>
        </svg>
        sirosid-{env}
      </a>
      <div class="navbar-links">
        <a href="/id/default/login">Login</a>
        <a href="/id/default/">Dashboard</a>
      </div>
    </div>
  </nav>

  <div class="content">
    <h1>Fly.io Environment: {env}</h1>
    <div class="subtitle">sirosid-dev &middot; ephemeral deployment &middot; <code>make fly-down ENV={env}</code> to tear down</div>

    <div class="card">
      <h2>Quick Links</h2>
      <div class="links">
        <a href="/id/default/login">Wallet Login</a>
        <a href="/id/default/">Wallet Dashboard</a>
        {f'<a href="{conformance_url}" target="_blank">OpenID Conformance Suite &#x2197;</a>' if conformance_url else ''}
      </div>
    </div>
{tabs_bar}
{status_panel_open}
    <div class="card">
      <h2>Environment Info</h2>
      <table>
        <tbody>
          <tr><td class="svc-name">Backend / API URL</td><td class="meta"><code>{backend_url}</code></td></tr>
          <tr><td class="svc-name">Frontend URL</td><td class="meta"><code>{frontend_url}</code></td></tr>
          <tr><td class="svc-name">WebAuthn RP ID</td><td class="meta"><code>{rp_id}</code></td></tr>
          <tr><td class="svc-name">Tenant ID</td><td class="meta"><code>default</code></td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h2>Native App Setup</h2>
      <div class="meta" style="margin-bottom:0.75rem">Point your native app's API base URL at Backend / API URL
        above, and its WebAuthn RP ID at the value above too - the ceremony origin must be in the list below
        (see scripts/android_apps.py / .android-apps to add your own debug or Play Store key).</div>
      <table>
        <thead><tr><th>Android package</th><th>Trusted origin</th></tr></thead>
        <tbody>{android_rows or '<tr><td colspan="2" class="meta">none configured</td></tr>'}</tbody>
      </table>
      <table style="margin-top:0.75rem">
        <thead><tr><th>iOS app ID (TEAMID.bundleid)</th></tr></thead>
        <tbody>{apple_rows or '<tr><td class="meta">none configured</td></tr>'}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>Services</h2>
      <table>
        <thead><tr><th>Service</th><th>Status</th><th>Details</th></tr></thead>
        <tbody id="svc-table"></tbody>
      </table>
    </div>
{status_panel_close}
{conformance_tab}
  </div>

  <div class="footer">
    <div class="footer-inner">
      <span>&copy; SIROS Foundation &middot; Auto-refreshes every 10s</span>
      <a href="#" onclick="checkAll();return false;">Refresh now</a>
    </div>
  </div>

<script>
var SERVICES = [
  {services_js}
];

var svcStatus = {{}};

function renderTable() {{
  var tbody = document.getElementById("svc-table");
  tbody.innerHTML = "";
  SERVICES.forEach(function(svc) {{
    var s = svcStatus[svc.name] || {{ state: "checking", detail: null }};
    var dotClass = s.state === "up" ? "dot-up" : s.state === "down" ? "dot-down" : "dot-checking";
    var label = s.state === "up" ? "running" : s.state === "down" ? "not reachable" : "checking\\u2026";
    var detail = "";
    if (s.detail) {{
      var parts = [];
      if (s.detail.service) parts.push(s.detail.service);
      if (s.detail.roles) parts.push("roles: " + s.detail.roles.join(", "));
      if (s.detail.capabilities) parts.push(s.detail.capabilities.length + " capabilities");
      if (s.detail.mode) parts.push("mode: " + s.detail.mode);
      detail = parts.join(" &middot; ");
    }}
    var tr = document.createElement("tr");
    tr.innerHTML =
      '<td class="svc-name">' + svc.name + ' <span class="port">:' + svc.port + '</span></td>' +
      '<td><span class="status-dot ' + dotClass + '"></span>' + label + '</td>' +
      '<td class="meta">' + detail + '</td>';
    tbody.appendChild(tr);
  }});
}}

function checkService(svc) {{
  svcStatus[svc.name] = {{ state: "checking", detail: null }};
  fetch(svc.check)
    .then(function(r) {{
      if (!r.ok) throw new Error(r.status);
      return r.text().then(function(text) {{
        try {{ return JSON.parse(text); }} catch(e) {{ return null; }}
      }});
    }})
    .then(function(data) {{
      svcStatus[svc.name] = {{ state: "up", detail: data }};
      renderTable();
    }})
    .catch(function() {{
      svcStatus[svc.name] = {{ state: "down", detail: null }};
      renderTable();
    }});
}}

function checkAll() {{ SERVICES.forEach(checkService); }}

// Unregister any service workers that might intercept navigation to /
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.getRegistrations().then(function(regs) {{
    regs.forEach(function(r) {{ r.unregister(); }});
  }});
}}

checkAll();
setInterval(checkAll, 10000);
{conformance_js}
</script>
{conformance_log_viewer_html}
</body>
</html>
"""


def merge_android_identities(wellknown_android: str, extra_identities: list | None = None) -> dict[str, list[str]]:
    """package -> [fingerprint_hex, ...], merging siros-id-stack's production
    `package::fingerprint,...` string with extra_identities (package,
    fingerprint) pairs (e.g. .android-apps) - shared by assetlinks_json()
    and the dashboard's Native App Setup card, so both show the exact same
    identities actually wired into rp_origins (see fly-up.py's
    generate_android_assets()/render_configs()) - one source of truth
    instead of two independent merges that could drift apart.
    """
    by_package: dict[str, list[str]] = {}
    for pair in wellknown_android.split(","):
        pair = pair.strip()
        if not pair or "::" not in pair:
            continue
        package, fingerprint = pair.split("::", 1)
        by_package.setdefault(package, []).append(fingerprint)

    for package, fingerprint in extra_identities or []:
        by_package.setdefault(package, [])
        if fingerprint not in by_package[package]:
            by_package[package].append(fingerprint)

    return by_package


def assetlinks_json(wellknown_android: str, extra_identities: list | None = None) -> str:
    """Build a Digital Asset Links JSON array from the same
    `package::fingerprint,...` string siros-id-stack's walletFrontend.
    wellknownAndroidPackageNamesAndFingerprints already carries (see
    scripts/fly-up.py - pulled straight from the rendered wallet-frontend-main
    ConfigMap), so already-published Play Store apps (wwwwallet, org.siros.id,
    ...) can validate against this Fly environment out of the box, not just
    a locally-built debug APK (unlike scripts/generate-assetlinks.sh, which
    is keyed of the developer's own debug keystore).

    extra_identities, if given, is a list of (package, fingerprint) pairs -
    e.g. several developers' own local debug keystores, or additional Play
    Store signing keys - added alongside the production ones (see
    fly-up.py's repeatable --android-app flag), so one environment can
    authenticate a mix of debug builds and Play Store builds at once. The
    same package can appear more than once with different fingerprints
    (e.g. a debug key and a Play Store upload key for the same app).
    """
    by_package = merge_android_identities(wellknown_android, extra_identities)
    entries = [
        {
            "relation": ["delegate_permission/common.handle_all_urls", "delegate_permission/common.get_login_creds"],
            "target": {
                "namespace": "android_app",
                "package_name": package,
                "sha256_cert_fingerprints": fingerprints,
            },
        }
        for package, fingerprints in by_package.items()
    ]
    return json.dumps(entries, indent=2)


