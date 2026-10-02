"""InstanceSpec - everything that distinguishes one sirosid-dev instance from
another, as plain data.

It holds CONTENT, never paths: a trusted-verifier root is the PEM text, not the
file it came from; the BBS key is the key, not `bbs_secret_key_file`. The CLI
(scripts/fly-up.py) reads those files and builds a spec; a service builds one
from a validated request. Either way the deploy code below it sees the same
thing and never touches a developer's disk to find out what to deploy.

`from_dict` is strict - an unknown key is an error, not ignored. That is
deliberate: the upstream chart silently ignoring unknown values keys produced
stacks that booted with the wrong config, and the same failure in a saved-config
format would be worse. `to_dict`/`from_dict` round-trip, which is what makes a
spec saveable.

What a spec does NOT decide: who may deploy what. Policy (image allowlists, URL
checks, quotas) belongs to the caller; `validate()` here only checks that the
spec is internally consistent.
"""
from dataclasses import dataclass, field, fields

# Keys of InstanceSpec that carry a secret. Callers that log or persist a spec
# for display must use redacted().
SECRET_FIELDS = ("bbs_secret_key",)


@dataclass
class InstanceSpec:
    env: str
    # Fly region, already resolved (pinned or detected). "" means "caller has
    # not decided yet"; deploying needs a non-empty value.
    region: str = ""
    # component name -> image reference, overriding the pinned defaults.
    images: dict = field(default_factory=dict)
    conformance: bool = False
    wallet_attestation: bool = False
    trusted_issuers: list = field(default_factory=list)
    trusted_verifiers: list = field(default_factory=list)
    # PEM text of CA certificates merged into the PDP's root pool.
    trusted_verifier_roots: list = field(default_factory=list)
    zk_circuits_sources: list = field(default_factory=list)
    # RICAL reader-trust list: URL and the PEM of the cert that signs it. Both
    # or neither.
    rical_provider_url: str = ""
    rical_root_pem: str = ""
    # "", "true" or "false": overrides verifier.digital_credentials.enable.
    dc_api_enable: str = ""
    # Ordered; later registries override earlier ones for the same vct/doctype.
    credential_registries: list = field(default_factory=list)
    # "package=fingerprint" entries, as accepted by scripts/android_apps.py.
    android_apps: list = field(default_factory=list)
    # Free-form chart values deep-merged last (the escape hatch).
    values: dict = field(default_factory=dict)
    # The issuer's blind-BBS secret key. Secret: see SECRET_FIELDS.
    bbs_secret_key: str = ""

    def validate(self, known_components=None):
        """Raise ValueError if the spec contradicts itself. Does not apply policy."""
        problems = []
        if not self.env:
            problems.append("env is required")
        if bool(self.rical_provider_url) != bool(self.rical_root_pem):
            problems.append("rical_provider_url and rical_root_pem must both be set, or neither")
        if self.dc_api_enable not in ("", "true", "false"):
            problems.append(f"dc_api_enable must be '', 'true' or 'false', got {self.dc_api_enable!r}")
        if known_components is not None:
            unknown = sorted(set(self.images) - set(known_components))
            if unknown:
                problems.append(f"images names unknown component(s): {', '.join(unknown)}")
        if problems:
            raise ValueError("invalid instance spec: " + "; ".join(problems))
        return self

    def to_dict(self, include_secrets=True):
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        if not include_secrets:
            for k in SECRET_FIELDS:
                if out.get(k):
                    out[k] = "<redacted>"
        return out

    def redacted(self):
        return self.to_dict(include_secrets=False)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError(f"instance spec must be a mapping, got {type(data).__name__}")
        known = {f.name: f for f in fields(cls)}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise ValueError(f"unknown instance spec key(s): {', '.join(unknown)}")
        kinds = {"images": dict, "values": dict, "conformance": bool, "wallet_attestation": bool,
                 "trusted_issuers": list, "trusted_verifiers": list, "trusted_verifier_roots": list,
                 "zk_circuits_sources": list, "credential_registries": list, "android_apps": list,
                 "env": str, "region": str, "rical_provider_url": str, "rical_root_pem": str,
                 "dc_api_enable": str, "bbs_secret_key": str}
        for key, value in data.items():
            if not isinstance(value, kinds[key]):
                raise ValueError(f"instance spec key {key!r} must be {kinds[key].__name__}, "
                                 f"got {type(value).__name__}")
        spec = cls(**{k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                      for k, v in data.items()})
        return spec
