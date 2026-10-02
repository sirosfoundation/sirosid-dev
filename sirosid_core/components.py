"""The component registry: what an instance is made of and in what order it is deployed.

Pure data. The two image pins that live in values-fly.yaml (mini-oidc and
env-admin are not in the chart) are PARAMETERS of build_components(), because a
library must not go and read a repo file to find out what to deploy - the CLI
reads values-fly.yaml and passes them in (scripts/fly_common.py).
"""

# Components whose data lives on a Fly volume. `make fly-down ENV=x
# KEEP_DATA=yes` leaves exactly these apps (machines stopped) so the next
# fly-up finds the data again; scripts/storage.py addresses their volumes.
STORAGE_APPS = ["mongodb", "conformance-mongodb"]
VOLUME_SIZE_GB = 1


def volume_name(component: str) -> str:
    """Fly volume names allow [a-z0-9_] only - no hyphens. One name per
    component (the app already scopes it), so `[mounts] source` in the
    generated fly.toml is stable across environments."""
    return component.replace("-", "_") + "_data"

# Last-resort fallback ONLY, for when Fly's own suggestion can't be reached
# (offline, or the header shape changed). The default is detect_region()
# below - contributors are in different places, and pinning everyone to one
# region put every environment several hundred kilometres from most of them.
FLY_REGION_FALLBACK = "arn"

# mini-oidc's OIDC client registered for vc-apigw's auth_providers.oidc (PID/EHIC
# issuance) - a single source of truth for both sides of this pairing:
# mini_oidc_config() (below) sets these as the client mini-oidc itself knows
# about, and render-helm-config.py feeds the SAME values explicitly into
# apigw's oidc config, instead of each independently hardcoding a literal that
# only works because it happens to match the other (previously: vc-config.yaml's
# base fixture hardcoded "apigw-oidc-client"/"test-secret", and this module's
# ${VAR:-default} fallbacks coincidentally matched it - nothing enforced that).
MINI_OIDC_APIGW_CLIENT_ID = "apigw-oidc-client"
MINI_OIDC_APIGW_CLIENT_SECRET = "test-secret"

# Deployment order matters - no `depends_on` equivalent on Fly, so components
# are deployed strictly in this order and each `fly deploy` blocks until its
# machine passes health checks before the next one starts.
def build_components(mini_oidc_image: str, env_admin_image: str) -> list:
    return [
        {
            "name": "mongodb",
            "image": "mongo:{mongo_version}",
            "ports": [{"internal": 27017, "public": False}],
            "checks": None,
            # Persistent: fly-up creates the volume if missing (ensure_volume) and
            # mounts it here, so users/passkeys/credentials survive redeploys,
            # image bumps and Fly host maintenance. The root password therefore
            # can no longer be rotated per run - see fly-up.py's
            # resolve_mongo_password().
            "mount": {"volume": volume_name("mongodb"), "destination": "/data/db"},
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
            # image_from_values components around it, and is read from
            # that file directly instead (see mini_oidc_image above).
            "image": mini_oidc_image,
            "ports": [{"internal": 9005, "public": True}],
            "checks": "/health",
        },
        {
            "name": "vc-registry",
            "image_from_values": "issuerRegistry",
            "ports": [{"internal": 8080, "public": True}],
            "checks": "/health",
        },
        {
            "name": "vc-issuer",
            "image_from_values": "issuerCore",
            # issuer-core's HTTP API is on 8081 (the chart renders api_server.addr
            # :8081; 8080 was never it - docker-compose.vc-services.yml publishes
            # 9000:8081 for the same reason). gRPC, what apigw actually calls, is
            # 8090. The check and the dashboard proxy probed 8080 for a long time,
            # which is why vc-issuer showed as the one unhealthy component and
            # every fly-up waited out wait_for_checks() on it.
            "ports": [{"internal": 8081, "public": False}, {"internal": 8090, "public": False}],
            "checks": None,
            # Internal-only (no [http_service]), so nothing previously blocked a
            # deploy on this actually becoming healthy before vc-verifier/vc-apigw
            # (which call it over 6PN) started deploying right after.
            "internal_check": {"type": "http", "port": 8081, "path": "/health"},
        },
        {
            "name": "vc-verifier",
            "image_from_values": "verifier",
            "ports": [{"internal": 8080, "public": True}],
            "checks": "/health",
        },
        {
            "name": "vc-apigw",
            "image_from_values": "issuerApigw",
            "ports": [{"internal": 8080, "public": True}],
            "checks": "/health",
        },
        {
            "name": "pdp",
            "image_from_values": "pdp",
            "ports": [{"internal": 8080, "public": False}],
            "checks": None,
            # Same reasoning as vc-issuer - wallet-backend calls pdp over 6PN
            # right after this, with nothing previously confirming it came up.
            "internal_check": {"type": "http", "port": 8080, "path": "/healthz"},
        },
        {
            "name": "wallet-backend",
            "image_from_values": "walletBackend",
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
            # sirosid-dev's own env-admin (env-admin/server.py): the privileged
            # actor behind the dashboard's "Clear all data", `make
            # fly-storage-clear` and the boot manager. Internal-only, reached via
            # wallet-frontend's /_admin/ proxy over 6PN exactly like
            # conformance-runner. It restarts this environment's Mongo consumers
            # through the Machines API with one app-scoped deploy token per
            # consumer (fly-up.py's env-admin branch) - never an org token.
            # Deployed right before wallet-frontend (whose nginx statically
            # resolves env-admin.internal at startup - see wallet_frontend_conf)
            # and after every consumer, since the tokens can only be minted for
            # apps that already exist.
            "name": "env-admin",
            "image": env_admin_image,
            "ports": [{"internal": 3002, "public": False}],
            "checks": None,
            "internal_check": {"type": "http", "port": 3002, "path": "/health"},
        },
        {
            "name": "wallet-frontend",
            "image_from_values": "walletFrontendConfig",
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
        "mount": {"volume": volume_name("conformance-mongodb"), "destination": "/data/db"},
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
