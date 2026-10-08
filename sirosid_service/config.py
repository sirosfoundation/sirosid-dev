"""Service configuration from the environment (12-factor: Fly sets these as env and secrets).

Secrets (FLY_API_TOKEN) are only ever read from the environment, never from a file in the
image or the repo.

The names: the console is `console.sirosid.dev` and that host is the WebAuthn RP ID.
Instances are SIBLINGS of it, `<component>-<id>.sirosid.dev`, reached only through the
shared edge (sirosid_core/edge.py). The hard rule, enforced here: no instance host may
ever be the console host or under it - any page there could use the console's passkeys.
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from sirosid_core.edge import INSTANCE_APP_PREFIX, EdgeConfigError, check_domain
from sirosid_core.naming import LAYOUT_APPS, LAYOUT_SINGLE_MACHINE, LAYOUTS, Naming

CONSOLE_HOST = "console.sirosid.dev"
INSTANCE_DOMAIN = "sirosid.dev"
_TRUE, _FALSE = ("1", "true", "yes"), ("0", "false", "no")


def _env(name, default=""):
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: str
    resources_root: Path
    fly_org: str
    fly_token: str
    rp_id: str
    rp_name: str
    origins: Tuple[str, ...]
    app_prefix: str
    host_pattern: str
    region: str
    tick_seconds: float
    client_ip_header: str
    max_instances: int
    host: str
    port: int
    console_dir: str
    sweep_grace_seconds: float = 3600.0
    # "apps" (one Fly app per component) or "single-machine" (one app `sid-<id>` per
    # instance, reached through the shared edge).
    layout: str = LAYOUT_APPS
    # The domain instance hosts live under ({component}-{id}.<domain>). "" = the
    # host pattern alone decides.
    instance_domain: str = ""
    # Single-machine only: public IPs on each instance app. Behind the edge: False.
    public_ips: bool = True
    openrouter_api_key: str = ""
    chat_models: Tuple[str, ...] = ()
    chat_user_daily_tokens: int = 300_000
    chat_global_daily_tokens: int = 3_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        root = Path(_env("SIROSID_RESOURCES", str(Path(__file__).resolve().parent.parent)))
        origins = tuple(o.strip() for o in _env("SIROSID_ORIGINS", f"https://{CONSOLE_HOST}").split(",") if o.strip())
        layout = _env("SIROSID_LAYOUT", LAYOUT_APPS)
        single = layout == LAYOUT_SINGLE_MACHINE
        instance_domain = _env("SIROSID_INSTANCE_DOMAIN", INSTANCE_DOMAIN if single else "")
        host_pattern = _env("SIROSID_HOST_PATTERN") or (
            "{component}-{id}." + instance_domain if instance_domain else "{app}.fly.dev")
        public_ips = _env("SIROSID_PUBLIC_IPS", "false" if single else "true").strip().lower()
        if public_ips not in _TRUE + _FALSE:
            raise SystemExit(f"configuration problems:\n  SIROSID_PUBLIC_IPS={public_ips!r}: true or false")
        s = cls(
            db_path=_env("SIROSID_DB", "/data/sirosid.db"), resources_root=root, fly_org=_env("SIROSID_FLY_ORG", "sirosdev"),
            fly_token=_env("FLY_API_TOKEN"), rp_id=_env("SIROSID_RP_ID", CONSOLE_HOST), rp_name=_env("SIROSID_RP_NAME", "SIROS ID Dev"),
            origins=origins, app_prefix=_env("SIROSID_APP_PREFIX", INSTANCE_APP_PREFIX), host_pattern=host_pattern,
            region=_env("SIROSID_REGION", "arn"), tick_seconds=float(_env("SIROSID_TICK_SECONDS", "300")),
            client_ip_header=_env("SIROSID_CLIENT_IP_HEADER", "Fly-Client-IP"),
            max_instances=int(_env("SIROSID_MAX_INSTANCES", "10")), host=_env("SIROSID_HOST", "0.0.0.0"), port=int(_env("PORT", "8080")),
            console_dir=_env("SIROSID_CONSOLE_DIR", str(root / "console")),
            sweep_grace_seconds=float(_env("SIROSID_SWEEP_GRACE_SECONDS", "3600")),
            layout=layout, instance_domain=instance_domain, public_ips=public_ips in _TRUE,
            openrouter_api_key=_env("OPENROUTER_API_KEY"),
            # With a key and no explicit list, let OpenRouter's auto router pick the model per request.
            chat_models=tuple(m.strip() for m in _env("SIROSID_CHAT_MODELS", "openrouter/auto" if _env("OPENROUTER_API_KEY") else "").split(",") if m.strip()),
            chat_user_daily_tokens=int(_env("SIROSID_CHAT_USER_DAILY_TOKENS", "300000")),
            chat_global_daily_tokens=int(_env("SIROSID_CHAT_GLOBAL_DAILY_TOKENS", "3000000")))
        s.validate()
        return s

    def validate(self):
        problems = []
        if not self.fly_token:
            problems.append("FLY_API_TOKEN is required (the service's own org-scoped token)")
        if not self.origins or any(not o.startswith("https://") for o in self.origins):
            problems.append("SIROSID_ORIGINS must be https origins")
        for o in self.origins:
            host = o.split("://", 1)[1].split("/")[0].split(":")[0]
            if not (host == self.rp_id or host.endswith("." + self.rp_id)):
                problems.append(f"origin {o} is not under the RP ID {self.rp_id}")
        if not any(f in self.host_pattern for f in ("{app}", "{env}", "{id}")):
            problems.append("SIROSID_HOST_PATTERN must contain {app}, {id} or {env}, or every instance would share a hostname")
        if self.layout not in LAYOUTS:
            problems.append(f"SIROSID_LAYOUT={self.layout!r}: one of {', '.join(LAYOUTS)}")
        if self.instance_domain:
            try:
                check_domain(self.instance_domain)
            except EdgeConfigError as e:
                problems.append(f"SIROSID_INSTANCE_DOMAIN: {e}")
        if self.layout == LAYOUT_SINGLE_MACHINE:
            flat = Naming("x", app_prefix=self.app_prefix, host_pattern=self.host_pattern,
                          layout=self.layout).flat_host_problem()
            if flat:
                problems.append(f"SIROSID_HOST_PATTERN: {flat}")
            if not self.public_ips and self.app_prefix != INSTANCE_APP_PREFIX:
                problems.append(f"SIROSID_APP_PREFIX must be {INSTANCE_APP_PREFIX!r} behind the edge, which replays "
                                f"<component>-<id>.<domain> to app {INSTANCE_APP_PREFIX}-<id>")
        # Nothing untrusted at or under the console host / RP ID.
        for name in sorted({self.rp_id, *(_origin_host(o) for o in self.origins)} - {""}):
            if instance_host_may_be(self.host_pattern, name):
                problems.append(f"instance hosts ({self.host_pattern}) could be {name} or under it: nothing untrusted may "
                                f"be served at or under the console's RP ID or origin - instances must be its siblings")
        if self.tick_seconds <= 0:
            problems.append("SIROSID_TICK_SECONDS must be positive")
        if self.sweep_grace_seconds < 60:
            # The sweeper destroys apps that look like ours but have no live row; the
            # grace is what lets a human notice (and a restored database catch up)
            # before anything is deleted. Shorten it for a test, never to nothing.
            problems.append("SIROSID_SWEEP_GRACE_SECONDS must be at least 60")
        if self.chat_models and not self.openrouter_api_key:
            problems.append("SIROSID_CHAT_MODELS is set but OPENROUTER_API_KEY is not")
        if self.chat_user_daily_tokens <= 0 or self.chat_global_daily_tokens <= 0:
            problems.append("the assistant's token budgets must be positive")
        if problems:
            raise SystemExit("configuration problems:\n  " + "\n  ".join(problems))


def _origin_host(origin: str) -> str:
    return origin.split("://", 1)[-1].split("/")[0].split(":")[0].lower()


# Conservative stand-ins for the host pattern's fields: wider than any real value,
# so a pattern that could EVER produce a name is caught.
_FIELD_RE = {"{component}": "[a-z0-9-]+", "{id}": "[a-z0-9]+", "{env}": "[a-z0-9-]+", "{app}": "[a-z0-9-]+"}


def instance_host_may_be(host_pattern: str, name: str) -> bool:
    """Whether some host from `host_pattern` could be `name` or a subdomain of it.
    An instance host is at/under `name` iff its last len(name) labels are name's
    labels, so compare from the right, each field standing for any LDH run."""
    labels = host_pattern.lower().split(".")
    target = name.lower().rstrip(".").split(".")
    if len(labels) < len(target):
        return False
    for pat, want in zip(labels[-len(target):], target):
        rx = re.escape(pat)
        for field, sub in _FIELD_RE.items():
            rx = rx.replace(re.escape(field), sub)
        if not re.fullmatch(rx, want):
            return False
    return True
