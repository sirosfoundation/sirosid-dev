"""Pure generators for the files and configs an instance's apps are deployed with.

fly.toml, the nginx configs, mini-oidc's config, the dashboard page and the
Android Digital Asset Links document. Each takes data and returns text (or
writes the one file it is given); none reads argv, the environment or the
developer's disk, and the names inside come from a Naming.
"""
import json
from pathlib import Path

from .android import hex_to_apk_key_hash
from .components import FLY_REGION_FALLBACK
from .naming import Naming


def write_fly_toml(path: Path, app: str, primary_public_port: int | None, process_cmd: str | None = None,
                    health_check_path: str | None = None, memory_mb: int = 256, cpus: int = 1,
                    internal_check: dict | None = None, tcp_passthrough_port: int | None = None,
                    region: str = "", mount: dict | None = None, autostart: bool = True):
    """Minimal per-app fly.toml - image/files/secrets are passed as `fly deploy`
    flags (see fly-up.py), not baked in here. Only the app-level shape
    (region, autostart/autostop, the one public port if any, and a command
    override where the image's own CMD isn't already correct - e.g. go-trust's
    Dockerfile CMD is ["serve"], which its flag-based CLI treats as a bare
    positional and then stops parsing, silently ignoring any flags after it -
    `--config` must be passed with no "serve" ahead of it) lives in the file.

    autostart=False (an instance that scales to zero as a unit) stops a stopped machine
    from waking itself on traffic; see lifecycle.start_instance.

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
        f"primary_region = '{region or FLY_REGION_FALLBACK}'",
        "",
    ]
    if process_cmd is not None:
        lines += [
            "[processes]",
            f"  app = '{process_cmd}'",
            "",
        ]
    if mount is not None:
        # A Fly volume (created by ensure_volume before the deploy - `fly
        # deploy` does not create volumes on its own). Pins the machine to
        # the volume's region; see fly-up.py's region guard.
        lines += [
            "[mounts]",
            f"  source = '{mount['volume']}'",
            f"  destination = '{mount['destination']}'",
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
            f"  auto_start_machines = {'true' if autostart else 'false'}",
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
            f"  cpus = {cpus}",
            f"  memory_mb = {memory_mb}",
            "",
        ]
    path.write_text("\n".join(lines))


def mini_oidc_config(env: str, naming: Naming = None) -> str:
    """mini-oidc's configs/config.production.yaml, baked into its image at
    /etc/mini-oidc/configs/config.production.yaml, re-stated here so the Fly
    deployment can pin the client ids/redirects. Because it REPLACES the
    image's file, every scope the image knows has to be repeated in
    scopes_supported - `ehic` (once missing upstream) and, since mini-oidc
    0.0.5, the EU Business Wallet attestation scopes eucc/eu_poa/ebw_oid
    (values-base.yaml's assertion-sourced types of the same names). mini-oidc
    does not refuse an unadvertised scope, so a stale list here only makes the
    discovery document lie. ${VAR} placeholders are expanded by
    mini-oidc's own binary from its container env at startup (see fly-up.py's
    env vars for this component) - this is the file's real content verbatim,
    not a Python-side template.
    """
    naming = naming or Naming(env)
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
    - eucc
    - eu_poa
    - ebw_oid

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


def wallet_proxy_conf(env: str, naming: Naming = None) -> str:
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
    naming = naming or Naming(env)
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

{wallet_proxy_locations(naming)}}}
"""


def wallet_proxy_locations(naming: Naming, assetlinks_path: str = "/etc/nginx/well-known/assetlinks.json",
                           forwarded_proto: str = "$scheme", real_ip: str = "$remote_addr") -> str:
    """The body of wallet-proxy's server block (see wallet_proxy_conf): what the
    single-machine front nginx serves for the wallet-proxy host. forwarded_proto
    / real_ip: what goes upstream as X-Forwarded-Proto / X-Real-IP - nginx's own
    view by default (the apps layout, unchanged), the forwarding edge's headers
    when the instance is only reachable through one (front_nginx_conf)."""
    http, admin = naming.addr("wallet-backend", "http"), naming.addr("wallet-backend", "admin")
    engine = naming.addr("wallet-backend", "engine")
    return f"""    # Matches go-wallet-backend's own MaxBodySize (pkg/middleware/bodysize.go)
    # - the private-data blob (S.credentials[] in the encrypted container)
    # grows unbounded as credentials accumulate, and mdoc/mDL credentials
    # each embed a base64 portrait photo. nginx's compiled-in default of 1m
    # was tight enough to 413 a real device after only a few mdoc
    # batch-issuance rounds against the same test account.
    client_max_body_size 10m;

    location /.well-known/assetlinks.json {{
        alias {assetlinks_path};
        default_type application/json;
    }}

    location = /admin/tenants {{
        proxy_pass http://{admin};
        proxy_set_header Host $host;
    }}

    location ~ ^/admin/tenants/[^/]+$ {{
        proxy_pass http://{admin};
        proxy_set_header Host $host;
    }}

    location ~ ^/admin/tenants/[^/]+/(issuers|verifiers)$ {{
        proxy_pass http://{admin};
        proxy_set_header Host $host;
    }}

    # Individual issuer/verifier resource (PUT to update client_id/client_jwk,
    # DELETE) - the collection-only match above doesn't cover these since it's
    # an exact ($) match, not a prefix.
    location ~ ^/admin/tenants/[^/]+/(issuers|verifiers)/[^/]+$ {{
        proxy_pass http://{admin};
        proxy_set_header Host $host;
    }}

    location /api/v2/wallet {{
        proxy_pass http://{engine};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP {real_ip};
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {forwarded_proto};
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }}

    location / {{
        proxy_pass http://{http};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP {real_ip};
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {forwarded_proto};
    }}
"""


# Public components of a single-machine instance that the front nginx proxies
# straight through to their own container (wallet-proxy is served by the front
# itself, see front_nginx_conf).
FRONT_PASSTHROUGH = ("mini-oidc", "vc-registry", "vc-verifier", "vc-apigw", "wallet-frontend")


def front_nginx_conf(naming: Naming, assetlinks_path: str = "/etc/nginx/well-known/assetlinks.json",
                     behind_edge: bool = False) -> str:
    """The single-machine layout's only public listener: one nginx on the
    machine's internal_port, routing by Host (one `server_name` per public
    component host) to the containers on 127.0.0.1.

    In the apps layout Fly's edge did this routing: each component had its own
    app and *.fly.dev name. One app has one fly.dev name, so the per-component
    hostnames come from `naming.host()` on a domain we route here (a wildcard
    record and certificate, or a shared edge that fly-replays to this app).

    wallet-proxy is folded in: its routes (wallet_proxy_locations, unchanged) are
    served for its own host, as the DEFAULT server - which is also what answers
    on <app>.fly.dev, so the admin routes the deploy registers issuers through
    work before any custom domain exists - and on a loopback port that
    wallet-frontend's same-origin API proxy calls, exactly as it called
    wallet-proxy.internal:8090 before. Headers Fly's edge set (X-Forwarded-*,
    Fly-Client-IP) pass through untouched: nginx forwards client headers.

    Never `depends_on` a slow container: the edge gives up on a machine whose
    service port is not answering within seconds of a start.

    behind_edge=True is the production shape: the app has no public IPs and a
    shared edge app fly-replays each request here, with the original Host and
    Fly-Client-IP intact. A request for any Host that is not one of this
    instance's own public names is refused (421) instead of falling through to
    a default. Measured on real Fly (2026-10-05, sirosid_core/edge.py):
    Fly-Client-IP is set by Fly's proxy (a client's own is replaced), so it is
    passed upstream as X-Real-IP; X-Forwarded-Proto is NOT - a client's
    `X-Forwarded-Proto: http` arrived here verbatim - so it is never trusted:
    the edge forces https, so the protocol behind it is always https. The
    public listener is IPv4-only on purpose: Fly's proxy reaches it over IPv4,
    while every other app on the org's default network can only reach it over
    6PN (IPv6) - where it is closed (verified: connection refused).
    """
    front = naming.port("front")
    if behind_edge:
        proto, real_ip, default = "https", "$http_fly_client_ip", ""
        trusted = ("        proxy_set_header X-Forwarded-Proto https;\n"
                   "        proxy_set_header X-Real-IP $http_fly_client_ip;\n")
    else:
        proto, real_ip, default, trusted = "$scheme", "$remote_addr", " default_server", ""
    blocks = [f"""# Single-machine front (sirosid_core.assets.front_nginx_conf) - generated, do not edit.
map $http_upgrade $sirosid_connection_upgrade {{
    default upgrade;
    ''      close;
}}
"""]
    if behind_edge:
        blocks.append(f"""server {{
    listen {front} default_server;
    server_name _;
    return 421;
}}
""")
    blocks.append(f"""server {{
    listen {front}{default};
    listen {naming.listen('wallet-proxy')};
    server_name {naming.host('wallet-proxy')};

{wallet_proxy_locations(naming, assetlinks_path, forwarded_proto=proto, real_ip=real_ip)}}}
""")
    for component in FRONT_PASSTHROUGH:
        blocks.append(f"""server {{
    listen {front};
    server_name {naming.host(component)};
    client_max_body_size 10m;

    location / {{
        proxy_pass http://{naming.addr(component)};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $sirosid_connection_upgrade;
{trusted}        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
""")
    return "\n".join(blocks)


def wallet_frontend_conf(env: str, conformance: bool = False, naming: Naming = None, env_admin: bool = True) -> str:
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
    naming = naming or Naming(env)
    # host:port as a sibling reaches each one (naming.addr) - .internal names in
    # the apps layout, 127.0.0.1 and the remapped ports in a single machine.
    backend, backend_admin = naming.addr("wallet-backend", "http"), naming.addr("wallet-backend", "admin")
    backend_engine = naming.addr("wallet-backend", "engine")
    wallet_proxy = naming.addr("wallet-proxy")
    pdp = naming.addr("pdp")
    mini_oidc = naming.addr("mini-oidc")
    vc_registry = naming.addr("vc-registry")
    vc_issuer = naming.addr("vc-issuer")
    vc_verifier = naming.addr("vc-verifier")
    vc_apigw = naming.addr("vc-apigw")
    conformance_server = f"{naming.internal('conformance-server')}"
    conformance_runner = f"{naming.internal('conformance-runner')}"
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
    # env-admin is optional: a hosted service has no credential to give it (see
    # InstanceSpec.env_admin), so the proxy, health check and Storage card go too.
    env_admin_name = naming.addr("env-admin")
    env_admin_health = (f"""    location = /_health/env-admin   {{ proxy_pass http://{env_admin_name}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}""") if env_admin else ""
    env_admin_block = (f"""    # env-admin (storage status + "Clear all data", see env-admin/server.py) -
    # mirrors nginx-e2e.conf's local /_admin/ block: same-origin, SSE-safe.
    # env-admin is always deployed (COMPONENTS), so like the health proxies
    # above this static target always resolves at nginx startup.
    location /_admin/ {{
        proxy_pass http://{env_admin_name}/;
        proxy_connect_timeout 5s;
        proxy_read_timeout 600s;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
    }}

    # The dashboard's Storage card - the same dashboard/storage-card.js the
    # local dashboard uses, uploaded by fly-up next to the dashboard HTML.
    location = /storage-card.js {{
        default_type application/javascript;
        alias /usr/share/nginx/storage-card.js;
        add_header Cache-Control "no-store" always;
    }}
""") if env_admin else ""
    return f"""server {{
    listen {naming.listen("wallet-frontend")};
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
    location = /_health/backend  {{ proxy_pass http://{backend}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/admin    {{ proxy_pass http://{backend_admin}/admin/status; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/engine   {{ proxy_pass http://{backend_engine}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/registry {{ proxy_pass http://{backend}/registry/status; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/pdp        {{ proxy_pass http://{pdp}/healthz; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/mini-oidc  {{ proxy_pass http://{mini_oidc}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-registry {{ proxy_pass http://{vc_registry}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-issuer   {{ proxy_pass http://{vc_issuer}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-verifier {{ proxy_pass http://{vc_verifier}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
    location = /_health/vc-apigw    {{ proxy_pass http://{vc_apigw}/health; proxy_connect_timeout 2s; proxy_read_timeout 2s; }}
{env_admin_health}
{conformance_health}
{conformance_proxy}
{env_admin_block}
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
        proxy_pass http://{wallet_proxy};
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
    location ~ ^/id/[^/]+/(.+)$ {{ 
      try_files /$1 /index.html; 
    }}

    # SPA fallback: every other /id/<tenant>/* route serves index.html.
    location / {{
        try_files $uri /index.html;
    }}
}}
"""


def wallet_frontend_dashboard_html(env: str, android_identities: dict[str, list[str]] | None = None,
                                    apple_app_ids: list[str] | None = None,
                                    conformance_url: str | None = None, naming: Naming = None,
                                    env_admin: bool = True) -> str:
    """Fly-adapted version of sirosid-dev's local startup.html landing page
    (same navbar/branding/Quick-Links/Services-table styling, reusing its
    CSS near-verbatim), served at wallet-frontend's own bare / (see
    wallet_frontend_conf()).

    What differs from the local version: the Conformance tab (SSE-driven
    runner UI + log viewer, ported verbatim) is only rendered when
    conformance_url is given (--conformance deploys conformance-runner, see
    CONFORMANCE_COMPONENTS), instead of always as locally. Build Info (git
    metadata from locally-built images) is dropped - nothing to show for a
    Fly environment pulling published images. The Services table stays,
    retargeted at this environment's actual deployed services (see SERVICES
    below) - mock-trust-pdp/mock-verifier are dropped (not deployed on Fly;
    pdp/vc-verifier are the real equivalents), mini-oidc/vc-registry are
    added (deployed on Fly, no local equivalent in the original list). The
    Storage card (dashboard/storage-card.js, one file for both dashboards)
    talks to env-admin through the same /_admin/ proxy as locally.

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
    naming = naming or Naming(env)
    backend_url = naming.url("wallet-proxy")
    frontend_url = naming.url("wallet-frontend")
    rp_id = f"{naming.host('wallet-frontend')}"
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
        ("wallet-backend", "backend", naming.port("wallet-backend", "http")),
        ("wallet-admin", "admin", naming.port("wallet-backend", "admin")),
        ("wallet-engine", "engine", naming.port("wallet-backend", "engine")),
        ("vctm-registry", "registry", naming.port("wallet-backend", "http")),
        ("pdp (go-trust)", "pdp", naming.port("pdp")),
        ("mini-oidc", "mini-oidc", naming.port("mini-oidc")),
        ("vc-registry", "vc-registry", naming.port("vc-registry")),
        ("vc-issuer", "vc-issuer", naming.port("vc-issuer")),
        ("vc-verifier", "vc-verifier", naming.port("vc-verifier")),
        ("vc-apigw", "vc-apigw", naming.port("vc-apigw")),
        *([("env-admin", "env-admin", naming.port("env-admin"))] if env_admin else []),
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

    storage_card_html = """    <div class="card" id="storage-card">
      <h2>Storage</h2>
      <div id="storage-body"><span class="meta">Checking env-admin&hellip;</span></div>
    </div>""" if env_admin else ""
    storage_card_script = '<script src="/storage-card.js"></script>' if env_admin else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{naming.label()}</title>
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
        {naming.label()}
      </a>
      <div class="navbar-links">
        <a href="/id/default/login">Login</a>
        <a href="/id/default/">Dashboard</a>
      </div>
    </div>
  </nav>

  <div class="content">
    <h1>Fly.io Environment: {env}</h1>
    <div class="subtitle">sirosid-dev &middot; Mongo data persists on a Fly volume across redeploys &middot; <code>make fly-down ENV={env}</code> to tear down (<code>KEEP_DATA=yes</code> keeps the volume)</div>

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

{storage_card_html}
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
{storage_card_script}
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
