"""Naming - every name an instance has, derived in one place.

An instance's components get names in four different namespaces, and they must
be able to vary independently:

  app       the Fly app name ("sirosid-<env>-<component>"). Global across ALL Fly
            orgs, so a service must generate ids that cannot collide with, or
            squat on, anyone else's.
  internal  the private-network address, always "<app>.internal" - derived, never
            chosen.
  host      the PUBLIC hostname users and wallets see. Today "<app>.fly.dev";
            for a hosted service it is a generated name on a domain we own. It is
            the one that appears in passkey rp_ids, OAuth redirect URIs and
            issuer identifiers, so it must come from here and nowhere else.
  network   the per-instance private network segment.

`Naming(env)` reproduces the scheme sirosid-dev has always used, byte for byte;
tests/test_fly_up_characterization.py holds that. Change a field to change the
scheme: a different `host_pattern` moves every public URL at once.

`layout` says how the instance is laid out on Fly:

  apps            one Fly app per component (the historical layout). Components
                  reach each other at "<app>.internal:<the image's own port>".
  single-machine  ONE Fly app with ONE multi-container machine
                  (sirosid_core/singlemachine.py). Containers share localhost, so
                  every component listens on its own port (SINGLE_MACHINE_PORTS)
                  and reaches the others at 127.0.0.1. The Fly app is
                  `machine_app()`; `app(component)` still names a component (it
                  feeds `{app}` in the host pattern) but is not a Fly app.

`addr(component, kind)` is the one way to spell "where does X listen, seen from
a sibling": every call site that used to write f"{naming.internal(x)}:8080"
goes through it, so the two layouts cannot drift apart.
"""
from dataclasses import dataclass

LAYOUT_APPS = "apps"
LAYOUT_SINGLE_MACHINE = "single-machine"
LAYOUTS = (LAYOUT_APPS, LAYOUT_SINGLE_MACHINE)

# The port each component listens on in the apps layout: whatever its image (or
# our config) uses by default. The first kind is the default one.
APPS_PORTS = {
    "mongodb": {"tcp": 27017},
    "mini-oidc": {"http": 9005},
    "vc-registry": {"http": 8080, "grpc": 8090},
    "vc-issuer": {"http": 8081, "grpc": 8090},
    "vc-verifier": {"http": 8080},
    "vc-apigw": {"http": 8080},
    "pdp": {"http": 8080},
    "wallet-backend": {"http": 8080, "admin": 8081, "engine": 8082},
    "wallet-proxy": {"http": 8090},
    "env-admin": {"http": 3002},
    "wallet-frontend": {"http": 80},
    "conformance-mongodb": {"tcp": 27017},
    "conformance-server": {"http": 8080},
    "conformance-runner": {"http": 3001},
    "conformance": {"https": 8443},
}

# Single-machine layout: one network namespace, so every listener is unique
# (vc/pdp/wallet-backend all default to 8080 and both gRPC servers to 8090).
# wallet-proxy is not a container there: the front nginx serves its routes on its
# own port, and `front` is the machine's public internal_port.
SINGLE_MACHINE_PORTS = {
    "mongodb": {"tcp": 27017},
    "mini-oidc": {"http": 9005},
    "vc-registry": {"http": 8100, "grpc": 8190},
    "vc-issuer": {"http": 8101, "grpc": 8191},
    "vc-verifier": {"http": 8102},
    "vc-apigw": {"http": 8103},
    "pdp": {"http": 8104},
    "wallet-backend": {"http": 8110, "admin": 8111, "engine": 8112},
    "wallet-frontend": {"http": 8120},
    "wallet-proxy": {"http": 8130},
    "env-admin": {"http": 3002},
    "front": {"http": 8080},
}


@dataclass(frozen=True)
class Naming:
    # The instance's identity: the CLI's environment name, or a generated id.
    env: str
    app_prefix: str = "sirosid"
    # Format string for the public hostname. Fields: {app}, {env} (alias {id}),
    # {component}. A single-machine instance needs FLAT names - every
    # placeholder in the first label, e.g. "{component}-{id}.sid.example" - so
    # one one-label wildcard certificate covers every instance (check_flat_host_pattern).
    host_pattern: str = "{app}.fly.dev"
    # "apps" (default: one Fly app per component) or "single-machine".
    layout: str = LAYOUT_APPS

    def __post_init__(self):
        if self.layout not in LAYOUTS:
            raise ValueError(f"unknown layout {self.layout!r} (one of: {', '.join(LAYOUTS)})")

    @property
    def single_machine(self) -> bool:
        return self.layout == LAYOUT_SINGLE_MACHINE

    def machine_app(self) -> str:
        """The one Fly app of a single-machine instance. Never collides with an
        apps-layout name: those always carry a component suffix."""
        return f"{self.app_prefix}-{self.env}"

    def port(self, component: str, kind: str = "") -> int:
        ports = (SINGLE_MACHINE_PORTS if self.single_machine else APPS_PORTS)[component]
        return ports[kind] if kind else next(iter(ports.values()))

    def addr(self, component: str, kind: str = "") -> str:
        """host:port of a component as a SIBLING reaches it - never a public URL."""
        host = "127.0.0.1" if self.single_machine else self.internal(component)
        return f"{host}:{self.port(component, kind)}"

    def flat_host_problem(self) -> str:
        """Why host_pattern cannot serve a single-machine instance, or "".

        One app has one *.fly.dev name, so each public component needs its own
        host on a domain routed to the app, and a TLS wildcard covers ONE label:
        the per-instance, per-component part must all sit in the first label
        ("{component}-{id}.sid.example", not "{component}.{id}.sid.example")."""
        first, _, domain = self.host_pattern.partition(".")
        if "{component}" not in first or not ("{id}" in first or "{env}" in first):
            return (f"host_pattern {self.host_pattern!r}: the first label must hold both {{component}} and "
                    f"{{id}} (e.g. '{{component}}-{{id}}.sid.example') so one wildcard certificate covers it")
        if not domain or "{" in domain:
            return f"host_pattern {self.host_pattern!r}: everything after the first label must be a fixed domain"
        if domain == "fly.dev" or domain.endswith(".fly.dev"):
            return (f"host_pattern {self.host_pattern!r}: Fly routes *.fly.dev names to the app of that name, "
                    f"which does not exist in this layout")
        return ""

    def listen(self, component: str, kind: str = "") -> str:
        """What a component's own listener binds. Single-machine: loopback only
        (Pilot's health checks come in over loopback - verified - and nothing
        outside the machine needs these ports; on an org's default network every
        other app could otherwise reach them). The apps layout: just the port."""
        port = self.port(component, kind)
        return f"127.0.0.1:{port}" if self.single_machine else str(port)

    def to_dict(self) -> dict:
        """What a service persists in plaintext so lifecycle works with nobody
        logged in. `layout` is omitted for the apps layout so rows written before
        it existed and rows written after compare equal."""
        out = {"env": self.env, "app_prefix": self.app_prefix, "host_pattern": self.host_pattern}
        if self.layout != LAYOUT_APPS:
            out["layout"] = self.layout
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Naming":
        return cls(d["env"], app_prefix=d.get("app_prefix", "sirosid"),
                   host_pattern=d.get("host_pattern", "{app}.fly.dev"), layout=d.get("layout", LAYOUT_APPS))

    def app(self, component: str) -> str:
        return f"{self.app_prefix}-{self.env}-{component}"

    def internal(self, component: str) -> str:
        return f"{self.app(component)}.internal"

    def host(self, component: str) -> str:
        # {id} is {env} under the name a hosted service uses for it.
        return self.host_pattern.format(app=self.app(component), env=self.env, id=self.env, component=component)

    def url(self, component: str) -> str:
        return f"https://{self.host(component)}"

    def network(self) -> str:
        return f"{self.app_prefix}-{self.env}"

    def label(self) -> str:
        """Human-readable instance label used in generated config names."""
        return f"{self.app_prefix}-{self.env}"
