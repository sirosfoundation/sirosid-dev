"""The shared EDGE: one public Fly app in front of every single-machine instance.

Production shape (see CLAUDE.md, "single-machine layout"):

  console.<domain>                the control plane - its own Fly app, NOT the edge's
  <domain>, www.<domain>          a small static site, served by the edge itself
  <component>-<id>.<domain>       an instance's public component. The edge answers
                                  with `fly-replay: app=sid-<id>;...` and Fly's proxy
                                  re-sends the request to that app (no public IPs, the
                                  org's default network), Host/Fly-Client-IP/
                                  X-Forwarded-Proto intact.
  anything else                   404 (default_server)

The edge holds ONE wildcard certificate `*.<domain>` plus the apex; instances hold
none. It never proxies a byte of instance traffic itself: a replay is decided on
the request line and headers, so request bodies and websocket upgrades are
carried by Fly's proxy, not by this nginx (which must therefore never answer an
Upgrade itself - it only ever returns the replay response or an error page).

Facts from a real-Fly spike that shaped the config (do not "simplify" them away):

* The fly-replay header is built by a `map` and is EMPTY unless the Host is
  exactly one instance shape, and it is emitted with plain `add_header` (2xx/3xx
  only) - never `add_header ... always`, which would also put it on 404/503 and
  replay error responses.
* A stopped target answers ~35 s later with a 503 unless the replay carries
  `timeout=...;fallback=prefer_self`: then Fly gives up after the timeout and
  re-sends the request to the edge with a `fly-replay-failed` header, which gets
  a clear 503 page here (and no replay header, so there is no loop).
* nginx's $host is lower-cased and loses a port and a trailing dot; the raw Host
  header is checked separately so `x.<domain>.` and `x.<domain>:443` do not map.
* Cross-network replay is refused by Fly: instances stay on the org's default
  network. A replay is followed at most ~2 hops. The edge's own app name must not
  start with `sid-` (that prefix belongs to instances; EDGE_RESERVED_PREFIX).

Pure: strings in, strings out. No I/O, standard library only.
"""
import re

# Instance apps are "sid-<id>"; the edge (and anything else) must never be.
INSTANCE_APP_PREFIX = "sid"
EDGE_RESERVED_PREFIX = INSTANCE_APP_PREFIX + "-"
# What an instance id looks like (the service generates base32 lowercase; any
# [a-z0-9] is accepted so the shape, not the generator, is the contract).
INSTANCE_ID_RE = "[a-z0-9]{8}"
# The public components of a single-machine instance (singlemachine.PUBLIC_COMPONENTS;
# repeated here so this module imports nothing).
DEFAULT_COMPONENTS = ("wallet-frontend", "wallet-proxy", "vc-registry", "vc-verifier", "vc-apigw", "mini-oidc")
# First labels that must never route to an instance whatever the component list says.
RESERVED_LABELS = ("console", "www")

REPLAY_TIMEOUT = "10s"
LISTEN_PORT = 8080
HEALTH_PATH = "/_edge/healthz"
STATIC_ROOT = "/srv/site"

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")
_COMPONENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_APP_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    # The apex may never ask for a passkey (the console's RP ID is its own host).
    ("Permissions-Policy", "publickey-credentials-get=(), publickey-credentials-create=(), camera=(), "
                           "microphone=(), geolocation=()"),
    ("Strict-Transport-Security", "max-age=31536000"),
)


class EdgeConfigError(ValueError):
    pass


def check_domain(domain: str) -> str:
    """The instances' domain: lower-case LDH labels, at least two, no trailing
    dot, no port, nothing Fly routes itself. Returns it; raises EdgeConfigError."""
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
        raise EdgeConfigError(f"domain {domain!r}: must be a lower-case DNS name of at least two labels "
                              f"(letters, digits, inner hyphens; no trailing dot, port, wildcard or path)")
    if domain in ("fly.dev", "flycast", "internal") or domain.endswith((".fly.dev", ".internal", ".flycast")):
        raise EdgeConfigError(f"domain {domain!r}: Fly routes that name itself; the edge needs a domain we own")
    if domain.split(".", 1)[0] in RESERVED_LABELS:
        raise EdgeConfigError(f"domain {domain!r}: its first label is reserved ({', '.join(RESERVED_LABELS)})")
    return domain


def check_components(components) -> tuple:
    comps = tuple(components)
    if not comps:
        raise EdgeConfigError("no components")
    for c in comps:
        if not isinstance(c, str) or not _COMPONENT_RE.match(c) or c in RESERVED_LABELS:
            raise EdgeConfigError(f"component {c!r}: must be lower-case [a-z0-9-] and not reserved")
    if len(set(comps)) != len(comps):
        raise EdgeConfigError("duplicate component")
    return comps


def check_edge_app(app: str) -> str:
    if not isinstance(app, str) or not _APP_RE.match(app):
        raise EdgeConfigError(f"app {app!r}: not a valid Fly app name")
    if app.startswith(EDGE_RESERVED_PREFIX):
        raise EdgeConfigError(f"app {app!r}: the {EDGE_RESERVED_PREFIX!r} prefix is reserved for instances")
    return app


def instance_host_regex(domain: str, components=DEFAULT_COMPONENTS) -> str:
    """The ONE pattern deciding which Host is an instance host. Group 1 is the
    component, group 2 the instance id. Python `re` and nginx's PCRE read it the
    same way (tests/test_edge.py runs the Python side over an adversarial corpus)."""
    check_domain(domain)
    alts = "|".join(re.escape(c) for c in sorted(check_components(components), key=lambda c: (-len(c), c)))
    return rf"^({alts})-({INSTANCE_ID_RE})\.{re.escape(domain)}$"


# The raw Host header must be plain dotted LDH labels: no port, no trailing dot,
# no empty label. Case is left to $host (nginx lower-cases it).
RAW_HOST_REGEX = r"^[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$"


def replay_value(instance_id: str) -> str:
    return f"app={INSTANCE_APP_PREFIX}-{instance_id};timeout={REPLAY_TIMEOUT};fallback=prefer_self"


def _nginx_header_lines(indent: str) -> str:
    # `always` is fine HERE: these are the static site's own headers, never fly-replay.
    return "".join(f'{indent}add_header {k} "{v}" always;\n' for k, v in SECURITY_HEADERS)


def error_page_html(status: int, domain: str) -> str:
    """A self-contained error page (no CSS, no script, no double quotes - it is
    embedded in a quoted nginx string)."""
    if status == 503:
        title = "Instance not available"
        body = ("This development instance is stopped, starting or no longer exists. "
                "Start it from the console and try again in a minute.")
    else:
        title = "Not found"
        body = "There is nothing at this address."
    html = (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{title}</title></head>"
            f"<body><h1>{title}</h1><p>{body}</p><p><a href=https://console.{domain}/>console.{domain}</a></p>"
            f"</body></html>\n")
    assert '"' not in html and "\\" not in html and "$" not in html
    return html


def edge_nginx_conf(domain: str, components=DEFAULT_COMPONENTS, static_root: str = STATIC_ROOT,
                    port: int = LISTEN_PORT, max_body: str = "100m") -> str:
    """The edge's nginx config (http context: nginx:alpine's /etc/nginx/conf.d/default.conf)."""
    check_domain(domain)
    if not re.match(r"^/[A-Za-z0-9/_.-]*$", static_root) or ".." in static_root:
        raise EdgeConfigError(f"static_root {static_root!r}: an absolute path of plain characters")
    if not re.match(r"^[0-9]+[km]?$", max_body):
        raise EdgeConfigError(f"max_body {max_body!r}")
    host_re = instance_host_regex(domain, components)
    # Same pattern, the id as a named capture for the map's value.
    named = host_re.replace("^(", "^(?:", 1).replace(f")-({INSTANCE_ID_RE})", f")-(?<iid>{INSTANCE_ID_RE})", 1)
    named = "^1:" + named[1:]
    e404, e503 = error_page_html(404, domain), error_page_html(503, domain)
    _error_headers = ('        add_header Cache-Control "no-store" always;\n'
                      '        add_header Content-Security-Policy "default-src \'none\'; frame-ancestors \'none\'" always;\n'
                      '        add_header X-Content-Type-Options "nosniff" always;\n')
    return f"""# sirosid shared edge for {domain} (sirosid_core.edge.edge_nginx_conf) - generated, do not edit.
server_tokens off;

# 1 when the RAW Host header is plain dotted labels: no port, no trailing dot.
map $http_host $sirosid_edge_raw_ok {{
    default 0;
    "~{RAW_HOST_REGEX}" 1;
}}

# The fly-replay value: EMPTY unless the (lower-cased) Host is exactly
# <component>-<8 x [a-z0-9]>.{domain}. Emitted with plain add_header (2xx only):
# never `always`, or 404s and 503s would be replayed too.
map "$sirosid_edge_raw_ok:$host" $sirosid_edge_replay {{
    default "";
    "~{named}" "app={INSTANCE_APP_PREFIX}-$iid;timeout={REPLAY_TIMEOUT};fallback=prefer_self";
}}

# Instance hosts and anything unknown (the edge's own *.fly.dev name included).
server {{
    listen {port} default_server;
    server_name _;
    # The body is never read here (Fly's proxy carries it on replay); a limit
    # below the instance's own would refuse large uploads before the replay.
    client_max_body_size {max_body};
    error_page 404 @sirosid_404;
    error_page 503 @sirosid_503;

    location = {HEALTH_PATH} {{
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }}

    location / {{
        if ($sirosid_edge_replay = "") {{
            return 404;
        }}
        # Fly came back: the target did not answer within the replay timeout.
        if ($http_fly_replay_failed != "") {{
            return 503;
        }}
        add_header fly-replay $sirosid_edge_replay;
        return 204;
    }}

    # Error pages: never a fly-replay header here (the map would be empty for a
    # 404 anyway, and a 503 is the answer to a FAILED replay).
    location @sirosid_404 {{
        default_type text/html;
{_error_headers}        return 404 "{e404}";
    }}
    location @sirosid_503 {{
        default_type text/html;
{_error_headers}        add_header Retry-After "30" always;
        return 503 "{e503}";
    }}
}}

# The apex: a static site, served here. Nothing is proxied from it.
server {{
    listen {port};
    server_name {domain} www.{domain};
    root {static_root};
    index index.html;
    absolute_redirect off;               # /dir -> /dir/ stays relative: this nginx sits behind Fly's TLS
    client_max_body_size 1k;
    error_page 404 /404.html;
{_nginx_header_lines("    ")}
    location = {HEALTH_PATH} {{
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }}

    # Chrome honours /.well-known/webauthn (Related Origin Requests): a host that serves it lets the
    # origins it lists use this host as their RP ID. Nothing under /.well-known/ is ever published here.
    location ^~ /.well-known/ {{
        return 404;
    }}

    location / {{
        limit_except GET {{
            deny all;
        }}
        try_files $uri $uri/ =404;
    }}
}}
"""


def edge_fly_toml(app: str, region: str = "arn", port: int = LISTEN_PORT, memory_mb: int = 256) -> str:
    """fly.toml for the edge app (deployed with an image built from edge/Dockerfile)."""
    check_edge_app(app)
    if not re.match(r"^[a-z]{3}$", region):
        raise EdgeConfigError(f"region {region!r}")
    return f"""# sirosid shared edge (sirosid_core.edge.edge_fly_toml) - generated, do not edit.
app = "{app}"
primary_region = "{region}"

[http_service]
  internal_port = {port}
  force_https = true
  # The edge is the way in to every instance: never stop it.
  auto_stop_machines = "off"
  auto_start_machines = true
  min_machines_running = 1

  [http_service.concurrency]
    type = "requests"
    soft_limit = 500
    hard_limit = 1000

  [[http_service.checks]]
    grace_period = "5s"
    interval = "15s"
    method = "GET"
    path = "{HEALTH_PATH}"
    timeout = "2s"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = {memory_mb}
"""


def render_site(files: dict, domain: str) -> dict:
    """The static site with @CONSOLE_URL@ / @DOMAIN@ filled in. {name: str} -> {name: str}."""
    check_domain(domain)
    return {name: text.replace("@CONSOLE_URL@", f"https://console.{domain}").replace("@DOMAIN@", domain)
            for name, text in files.items()}


def dns_records(domain: str, app: str, ipv4: str = "", ipv6: str = "") -> list:
    """What the domain's DNS needs so the edge gets the apex and every instance
    host: [(name, type, value)]. The console's record is not the edge's business
    (it points at the console app) and is listed for completeness only."""
    check_domain(domain)
    check_edge_app(app)
    out = []
    for name in (domain, f"*.{domain}", f"www.{domain}"):
        if ipv4:
            out.append((name, "A", ipv4))
        if ipv6:
            out.append((name, "AAAA", ipv6))
        if not (ipv4 or ipv6):
            out.append((name, "CNAME", f"{app}.fly.dev") if name != domain else (name, "A/AAAA", f"<{app}'s IPs>"))
    return out


def cert_commands(domain: str, app: str) -> list:
    """The `flyctl certs add` commands for the edge (the apex, www and ONE wildcard).
    Printed, never run, for a domain whose DNS we do not control."""
    check_domain(domain)
    check_edge_app(app)
    return [f"flyctl certs add {name} -a {app}" for name in (domain, f"www.{domain}", f"*.{domain}")]


def route(host_header: str, domain: str, components=DEFAULT_COMPONENTS, replay_failed: bool = False):
    """A pure-Python mirror of what the generated nginx does with a Host header:
    ("replay", "sid-<id>") | ("static", None) | ("404", None) | ("503", None) |
    ("400", None). Uses the SAME regexes the config carries, and nginx's own
    $host normalisation (lower-case, port and one trailing dot stripped; a Host
    with '/', '\\', '..', a space or a control character is a 400)."""
    raw = host_header
    if raw is None:
        raw = ""
    if any(ch in raw for ch in ("/", "\\")) or ".." in raw or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        return ("400", None)
    host = raw.lower()
    if ":" in host and not host.startswith("["):     # an IPv6 literal never matches anyway
        host = host.split(":", 1)[0]
    if host.endswith("."):
        host = host[:-1]
    raw_ok = 1 if re.match(RAW_HOST_REGEX, raw) else 0
    if host in (domain, f"www.{domain}"):
        return ("static", None)
    m = re.match(instance_host_regex(domain, components), host) if raw_ok else None
    if not m:
        return ("404", None)
    if replay_failed:
        return ("503", None)
    return ("replay", f"{INSTANCE_APP_PREFIX}-{m.group(2)}")
