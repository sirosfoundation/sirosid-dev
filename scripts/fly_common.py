"""Shared component registry + flyctl helpers for scripts/fly-up.py and fly-down.py.

See scripts/render-helm-config.py's module docstring for the overall Fly
design (one Fly app per component, `sirosid-<env>-<component>` naming,
images pulled straight from helm-charts/siros-id-stack/values.yaml, no local
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
        # Not part of helm-charts (sirosid-dev/testing-only, see
        # docker-compose.vc-services.yml) - a minimal OIDC Provider standing
        # in for a real government/eIDAS IdP behind vc-apigw's OIDC auth
        # provider (pid/pid_1_5/pid_1_8/ehic issuance). Must be public - the
        # end user's browser/app is redirected here to log in. Only the `op`
        # role is deployed; `mini-oidc-rp` is a separate test-harness client
        # for exercising the OP standalone, not part of the real apigw flow.
        "name": "mini-oidc",
        "image": "ghcr.io/sirosfoundation/mini-oidc:main",
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
# CONFORMANCE=yes overlay - the real, functional piece of that overlay is
# just these 3 (the third component in the local compose file,
# "conformance-runner", is a documented placeholder with no actual API - see
# conformance-runner/Dockerfile's own comment - so there's nothing to port).
# vc-proxy (the local overlay's 4th component, a self-signed TLS front for
# otherwise-plain-HTTP vc-services) also isn't needed here: every Fly-public
# vc-service already has real TLS via Fly's own *.fly.dev certs, so the
# conformance suite can test against vc-apigw/vc-verifier's public URLs
# directly - no proxy required.
CONFORMANCE_COMPONENTS = [
    {
        "name": "conformance-mongodb",
        # Pinned to the same fixed tag docker-compose.conformance.yml uses -
        # a requirement of this specific (older) conformance suite version,
        # not templated from helm-charts' mongoCommunityVersion like the
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
    pdp = f"{app_name(env, 'pdp')}.internal"
    mini_oidc = f"{app_name(env, 'mini-oidc')}.internal"
    vc_registry = f"{app_name(env, 'vc-registry')}.internal"
    vc_issuer = f"{app_name(env, 'vc-issuer')}.internal"
    vc_verifier = f"{app_name(env, 'vc-verifier')}.internal"
    vc_apigw = f"{app_name(env, 'vc-apigw')}.internal"
    conformance_server = f"{app_name(env, 'conformance-server')}.internal"
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
</style>
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
</script>
</body>
</html>
"""


def merge_android_identities(wellknown_android: str, extra_identities: list | None = None) -> dict[str, list[str]]:
    """package -> [fingerprint_hex, ...], merging helm-charts' production
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
    `package::fingerprint,...` string helm-charts' walletFrontend.
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


