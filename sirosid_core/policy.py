"""Saved configs and the policy between "what a user asks for" and an InstanceSpec.

A SAVED CONFIG is the document a user (or their agent) edits, stores and re-uses.
It is deliberately smaller than an InstanceSpec: it carries no org, region,
naming, hostname pattern or scale-to-zero - those are the platform's, and a user
must not be able to choose them. `build_spec()` is the only way from one to the
other, and it is where policy lives:

  * typed keys only. An unknown key is an error, never ignored (the lesson of the
    upstream chart silently dropping unknown values).
  * every URL is https, public-looking and free of credentials, so an instance
    cannot be pointed at localhost, a private address or Fly's internal network.
  * images need a CAPABILITY. Choosing a component's image is choosing code that
    runs in our org, so it is granted per user (the dev team), never by default.
    Even with it a reference must be fully qualified, pinned by tag or digest, and
    not a bare local name (which would make the deploy shell out to a docker
    daemon) nor another app's Fly registry namespace.
  * raw chart `values` need their own capability: they reach every config key.
  * everything wrong is reported at once, as (path, message) pairs, so an agent
    can fix a config in one round trip instead of one error at a time.

This module does no I/O: it cannot check that a host resolves or that an image
exists. Those checks belong to the caller, which can resolve a tag to a digest
(see ImageRef.pinned) and refuse an unreachable registry.
"""
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .android import identities_from_entries
from .components import component_names
from .spec import InstanceSpec

SCHEMA_VERSION = 1

CAP_CUSTOM_IMAGES = "custom_images"
CAP_RAW_VALUES = "raw_values"
CAPABILITIES = (CAP_CUSTOM_IMAGES, CAP_RAW_VALUES)

MAX_LIST = 50
MAX_ANDROID = 20
MAX_URL = 2048
MAX_IDENTITY = 512
MAX_NAME = 64
MAX_PEM = 16 * 1024
MAX_PEMS = 10


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self):
        return f"{self.path}: {self.message}"


class PolicyError(ValueError):
    def __init__(self, problems: List[Problem]):
        self.problems = list(problems)
        super().__init__("; ".join(str(p) for p in self.problems))


@dataclass
class PlatformPolicy:
    """What THIS deployment of the service allows. Set by the operator, never by
    a user or their config."""
    # channel name -> {component: image}. "default" (no overrides: the pins in
    # values-fly.yaml) always exists. A channel is how an ordinary user chooses a
    # version without being allowed to name an arbitrary image.
    channels: Dict[str, Dict[str, str]] = field(default_factory=lambda: {"default": {}})
    allow_conformance: bool = False        # v1: conformance stays out
    allowed_image_registries: Optional[Tuple[str, ...]] = None   # None = any public registry
    region: str = "arn"
    app_prefix: str = "sid"
    host_pattern: str = "{app}.fly.dev"
    scale_to_zero: bool = True
    env_admin: bool = True                 # a hosted service sets False (see InstanceSpec.env_admin)
    # "apps" or "single-machine" (one Fly app, one multi-container machine per
    # instance: sirosid_core/singlemachine.py). Single-machine needs a FLAT
    # host_pattern ("{component}-{id}.<instances domain>") and env_admin=False.
    layout: str = "apps"
    # Single-machine only: public IPs on each instance app. A service behind a
    # shared fly-replay edge sets False (see InstanceSpec.public_ips).
    public_ips: bool = True


# --- the schema ------------------------------------------------------------

# key -> (json type, description). Hand-written so it can be handed to an agent as
# the contract; tests/test_policy.py fails if it drifts from what build_spec accepts.
SAVED_CONFIG_KEYS = {
    "schema_version": ("integer", "Must be 1."),
    "name": ("string", f"A label for this config (<= {MAX_NAME} chars)."),
    "channel": ("string", "Which released versions to run; see PlatformPolicy.channels. Default 'default'."),
    "trusted_issuers": ("array", "https URLs of extra issuers this instance's PDP should trust."),
    "trusted_verifiers": ("array", "Extra verifier identities to trust: an https URL, or x509_hash:/x509_san_dns:/x509_san_uri:."),
    "trusted_verifier_roots": ("array", "PEM text of CA certificates to add to the PDP's root pool."),
    "credential_registries": ("array", "https base URLs of credential-type registries to resolve metadata from."),
    "android_apps": ("array", "'package=SHA256 fingerprint' entries to trust for passkeys."),
    "wallet_attestation": ("boolean", "Enable wallet-attestation based client authentication."),
    "dc_api_enable": ("string", "'', 'true' or 'false': override W3C DC API support."),
    "images": ("object", "component -> image reference. Needs the custom_images capability."),
    "values": ("object", "Raw chart values, deep-merged last. Needs the raw_values capability."),
    "conformance": ("boolean", "Also deploy the OpenID conformance suite (if the platform allows it)."),
}


def schema() -> dict:
    """JSON Schema for a saved config, for MCP tool listings and editors."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "sirosid saved config", "type": "object", "additionalProperties": False,
        "properties": {k: {"type": t, "description": d} for k, (t, d) in SAVED_CONFIG_KEYS.items()},
        "x-capabilities": {"images": CAP_CUSTOM_IMAGES, "values": CAP_RAW_VALUES},
    }


# --- URL and identity checks --------------------------------------------------

_BAD_SUFFIXES = (".internal", ".local", ".localhost", ".lan", ".home", ".corp", ".flycast")
_IDENTITY_PREFIXES = ("x509_hash:", "x509_san_dns:", "x509_san_uri:", "decentralized_identifier:")
_IDENTITY_CHARS = re.compile(r"^[A-Za-z0-9:._~/%+=@,-]+$")


def check_https_url(value, path, problems: List[Problem]) -> bool:
    """https, a real-looking public host, no credentials, no private address."""
    if not isinstance(value, str) or not value:
        problems.append(Problem(path, "must be a non-empty string"))
        return False
    if len(value) > MAX_URL:
        problems.append(Problem(path, f"is longer than {MAX_URL} characters"))
        return False
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        problems.append(Problem(path, "is not a valid URL"))
        return False
    if u.scheme != "https":
        problems.append(Problem(path, "must be an https URL"))
        return False
    if u.username or u.password:
        problems.append(Problem(path, "must not contain credentials"))
        return False
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        problems.append(Problem(path, "has no host"))
        return False
    if port is not None and not (0 < port < 65536):
        problems.append(Problem(path, "has an invalid port"))
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            problems.append(Problem(path, "must not be a private, loopback or reserved address"))
            return False
        return True
    if host == "localhost" or "." not in host or host.endswith(_BAD_SUFFIXES):
        problems.append(Problem(path, "must be a public host name (not localhost, an internal or single-label name)"))
        return False
    return True


def check_identity(value, path, problems: List[Problem]) -> bool:
    if not isinstance(value, str) or not value:
        problems.append(Problem(path, "must be a non-empty string"))
        return False
    if value.startswith("https://"):
        return check_https_url(value, path, problems)
    if len(value) > MAX_IDENTITY:
        problems.append(Problem(path, f"is longer than {MAX_IDENTITY} characters"))
        return False
    if not value.startswith(_IDENTITY_PREFIXES) or not _IDENTITY_CHARS.match(value):
        problems.append(Problem(path, "must be an https URL or start with one of: " + ", ".join(_IDENTITY_PREFIXES)))
        return False
    return True


def check_pem(value, path, problems: List[Problem]) -> bool:
    if not isinstance(value, str) or len(value) > MAX_PEM:
        problems.append(Problem(path, f"must be PEM text of at most {MAX_PEM} bytes"))
        return False
    body = value.strip()
    if not (body.startswith("-----BEGIN CERTIFICATE-----") and body.endswith("-----END CERTIFICATE-----")):
        problems.append(Problem(path, "must be a PEM CERTIFICATE (and nothing else - no private keys)"))
        return False
    if "PRIVATE KEY" in body:
        problems.append(Problem(path, "must not contain a private key"))
        return False
    return True


# --- image references ---------------------------------------------------------

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE = re.compile(
    r"^(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?)/"
    r"(?P<repo>" + _COMPONENT + r"(?:/" + _COMPONENT + r")*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[a-f0-9]{64}))?$")


@dataclass(frozen=True)
class ImageRef:
    host: str
    repo: str
    tag: str = ""
    digest: str = ""

    @property
    def pinned(self) -> bool:
        return bool(self.digest)

    def __str__(self):
        return f"{self.host}/{self.repo}" + (f":{self.tag}" if self.tag else "") + (f"@{self.digest}" if self.digest else "")


def parse_image(ref: str) -> ImageRef:
    """Parse a fully qualified image reference or raise ValueError saying why not."""
    if not isinstance(ref, str) or not ref or len(ref) > 300:
        raise ValueError("must be a non-empty image reference")
    bare = ("must name its registry (e.g. ghcr.io/org/image:tag) - a bare name would be looked up on "
            "the deploying machine's local docker daemon")
    if "/" not in ref:
        raise ValueError(bare)
    m = _IMAGE.match(ref)
    if not m:
        raise ValueError("is not a valid fully qualified image reference (registry/repo:tag or @sha256:...)")
    host = m["host"]
    first = host.split(":")[0]
    if "." not in first and ":" not in host:
        raise ValueError(bare)
    if not (m["tag"] or m["digest"]):
        raise ValueError("must have a tag or a digest")
    return ImageRef(host, m["repo"], m["tag"] or "", m["digest"] or "")


_FORBIDDEN_REGISTRIES = ("registry.fly.io",)


def check_image(ref, path, policy: PlatformPolicy, problems: List[Problem]) -> bool:
    try:
        parsed = parse_image(ref)
    except ValueError as e:
        problems.append(Problem(path, str(e)))
        return False
    host = parsed.host.split(":")[0]
    if host in _FORBIDDEN_REGISTRIES or host == "localhost" or host.endswith(_BAD_SUFFIXES):
        problems.append(Problem(path, f"registry {parsed.host!r} is not allowed (another app's images, or not public)"))
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            problems.append(Problem(path, "registry must not be a private address"))
            return False
    except ValueError:
        pass
    allowed = policy.allowed_image_registries
    if allowed is not None and parsed.host not in allowed:
        problems.append(Problem(path, f"registry {parsed.host!r} is not one of: {', '.join(allowed)}"))
        return False
    return True


# --- the policy ----------------------------------------------------------------

def _list_of(data, key, problems, limit=MAX_LIST):
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(Problem(key, "must be an array"))
        return []
    if len(value) > limit:
        problems.append(Problem(key, f"has more than {limit} entries"))
        return []
    return value


def validate(saved: dict, capabilities=(), policy: PlatformPolicy = None) -> List[Problem]:
    """Every problem with `saved` for a user holding `capabilities`; empty means fine."""
    policy = policy or PlatformPolicy()
    caps = set(capabilities)
    problems: List[Problem] = []
    if not isinstance(saved, dict):
        return [Problem("$", "a saved config must be a JSON object")]
    unknown = sorted(set(saved) - set(SAVED_CONFIG_KEYS))
    for k in unknown:
        problems.append(Problem(k, "is not a known key (see schema()); unknown keys are rejected, not ignored"))
    version = saved.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION or isinstance(version, bool):
        problems.append(Problem("schema_version", f"must be {SCHEMA_VERSION}"))
    name = saved.get("name", "")
    if not isinstance(name, str) or len(name) > MAX_NAME:
        problems.append(Problem("name", f"must be a string of at most {MAX_NAME} characters"))

    channel = saved.get("channel", "default")
    if not isinstance(channel, str) or channel not in policy.channels:
        problems.append(Problem("channel", f"must be one of: {', '.join(sorted(policy.channels))}"))

    for i, v in enumerate(_list_of(saved, "trusted_issuers", problems)):
        check_https_url(v, f"trusted_issuers[{i}]", problems)
    for i, v in enumerate(_list_of(saved, "trusted_verifiers", problems)):
        check_identity(v, f"trusted_verifiers[{i}]", problems)
    for i, v in enumerate(_list_of(saved, "credential_registries", problems)):
        check_https_url(v, f"credential_registries[{i}]", problems)
    for i, v in enumerate(_list_of(saved, "trusted_verifier_roots", problems, MAX_PEMS)):
        check_pem(v, f"trusted_verifier_roots[{i}]", problems)
    apps = _list_of(saved, "android_apps", problems, MAX_ANDROID)
    if apps:
        if not all(isinstance(a, str) for a in apps):
            problems.append(Problem("android_apps", "entries must be strings"))
        else:
            try:
                identities_from_entries(apps)
            except ValueError as e:
                problems.append(Problem("android_apps", str(e)))
            except Exception:  # a malformed fingerprint, not a code error
                problems.append(Problem("android_apps", "has a malformed fingerprint"))

    for key in ("wallet_attestation", "conformance"):
        if key in saved and not isinstance(saved[key], bool):
            problems.append(Problem(key, "must be true or false"))
    if saved.get("conformance") is True and not policy.allow_conformance:
        problems.append(Problem("conformance", "is not available on this platform"))
    elif saved.get("conformance") is True and policy.layout == "single-machine":
        problems.append(Problem("conformance", "is not available in the single-machine layout"))
    if saved.get("dc_api_enable", "") not in ("", "true", "false"):
        problems.append(Problem("dc_api_enable", "must be '', 'true' or 'false'"))

    images = saved.get("images")
    if images:
        if CAP_CUSTOM_IMAGES not in caps:
            problems.append(Problem("images", f"needs the {CAP_CUSTOM_IMAGES!r} capability, which your account does not have"))
        elif not isinstance(images, dict):
            problems.append(Problem("images", "must be an object of component -> image"))
        else:
            known = set(component_names())
            for comp, ref in images.items():
                if comp not in known:
                    problems.append(Problem(f"images.{comp}", f"is not a component (one of: {', '.join(sorted(known))})"))
                else:
                    check_image(ref, f"images.{comp}", policy, problems)
    elif images is not None and not isinstance(images, dict):
        problems.append(Problem("images", "must be an object of component -> image"))

    values = saved.get("values")
    if values:
        if CAP_RAW_VALUES not in caps:
            problems.append(Problem("values", f"needs the {CAP_RAW_VALUES!r} capability, which your account does not have"))
        elif not isinstance(values, dict):
            problems.append(Problem("values", "must be an object"))
    return problems


# The InstanceSpec fields that belong to the platform and to the instance's existence on
# Fly, never to a saved config. A reconfigure keeps them from the deployed spec: changing
# the layout, prefix or host pattern would deploy a SECOND instance beside the first, and
# a Mongo volume pins its app to its region.
PLATFORM_FIELDS = ("env", "region", "app_prefix", "host_pattern", "scale_to_zero", "env_admin", "layout", "public_ips")


def rebuild_spec(saved: dict, deployed: InstanceSpec, capabilities=(), policy: PlatformPolicy = None) -> InstanceSpec:
    """The spec for applying `saved` to an instance that already exists as `deployed`.

    Validated against `capabilities` and the platform policy exactly as build_spec does,
    but with the policy's layout taken from the instance (the platform may have switched
    layouts since it was created), and every PLATFORM_FIELDS value copied from `deployed`.
    Raises PolicyError listing every problem."""
    from dataclasses import replace
    policy = replace(policy or PlatformPolicy(), layout=deployed.layout)
    spec = build_spec(saved, deployed.env, capabilities, policy)
    return replace(spec, **{f: getattr(deployed, f) for f in PLATFORM_FIELDS}).validate(component_names())


def build_spec(saved: dict, env: str, capabilities=(), policy: PlatformPolicy = None) -> InstanceSpec:
    """The InstanceSpec for a validated saved config, with the platform's own
    settings applied. Raises PolicyError listing every problem."""
    policy = policy or PlatformPolicy()
    problems = validate(saved, capabilities, policy)
    if problems:
        raise PolicyError(problems)
    channel_images = dict(policy.channels.get(saved.get("channel", "default"), {}))
    images = {**channel_images, **(saved.get("images") or {})}
    values = dict(saved.get("values") or {})
    return InstanceSpec(
        env=env,
        region=policy.region,
        images=images,
        conformance=bool(saved.get("conformance", False)),
        wallet_attestation=bool(saved.get("wallet_attestation", False)),
        trusted_issuers=list(saved.get("trusted_issuers") or []),
        trusted_verifiers=list(saved.get("trusted_verifiers") or []),
        trusted_verifier_roots=[p.strip() + "\n" for p in saved.get("trusted_verifier_roots") or []],
        credential_registries=list(saved.get("credential_registries") or []),
        android_apps=list(saved.get("android_apps") or []),
        dc_api_enable=saved.get("dc_api_enable", ""),
        values=values,
        app_prefix=policy.app_prefix,
        host_pattern=policy.host_pattern,
        scale_to_zero=policy.scale_to_zero,
        env_admin=policy.env_admin,
        layout=policy.layout,
        public_ips=policy.public_ips,
    ).validate(component_names())
