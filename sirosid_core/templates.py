"""Starting-point configs, so nobody begins from a blank page.

A template is a saved config (the same document `policy.validate` checks) plus words about it.
Nothing here is special to the service: a template is data a user copies, edits and saves like any
config, and every one of them must pass `policy.validate` for a user with the capabilities it
`requires` (tests/test_templates.py enforces that, so a template cannot rot behind a policy change).

Templates are ordinary unprivileged configs unless `requires` says otherwise. They never contain a
secret, a URL that is not public, or a value that only works for one person.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .policy import CAP_CUSTOM_IMAGES

REGISTRY_URL = "https://registry.siros.org"
WALLET_BACKEND_FALLBACK = "ghcr.io/sirosfoundation/go-wallet-backend:main"


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    description: str
    config: dict
    hints: Tuple[str, ...] = ()
    requires: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description, "config": dict(self.config),
                "hints": list(self.hints), "requires": list(self.requires)}


def config_templates(resources=None) -> List[Template]:
    """The templates, in the order they are offered. `resources` (a sirosid_core.resources.Resources) lets
    the custom-image template start from the wallet-backend image this deployment currently pins."""
    pin = WALLET_BACKEND_FALLBACK
    if resources is not None:
        pin = resources.image_pin("walletBackend", pin)
    return [
        Template(
            "standard", "Standard test stack",
            "The whole SIROS ID stack at the current released versions: wallet (web), wallet backend, issuer, verifier, "
            "credential registry, trust service and a test OIDC login. Credential types come from the definitions shipped "
            "with the platform. The right place to start when you just need a working wallet, issuer and verifier.",
            {"schema_version": 1},
            ("Add `android_apps` entries ('package=SHA-256 cert fingerprint', as printed by keytool) to let an Android app use "
             "passkeys against this instance.",
             "Add `trusted_issuers` / `trusted_verifiers` to make this instance's trust service accept a partner's issuer or verifier.")),
        Template(
            "siros-registry", "Credential types from the SIROS registry",
            "As the standard stack, but every SD-JWT credential type is resolved from registry.siros.org instead of the "
            "definitions shipped with the platform. The issuer then advertises each type's real `vct` (for example "
            "urn:eudi:pid:arf-1.8:1), which wallets that check the type against the registry require. Use it to test "
            "against the same metadata as production-like wallets.",
            {"schema_version": 1, "credential_registries": [REGISTRY_URL]},
            ("mdoc credential types stay on the platform's own definitions; only SD-JWT types come from the registry.",)),
        Template(
            "wallet-attestation", "Wallet attestation",
            "Turns on wallet-attestation based client authentication (a wallet instance attestation, WIA) between the wallet "
            "and the issuer. Use it to test a wallet app that proves what it is before it is issued credentials.",
            {"schema_version": 1, "wallet_attestation": True},
            ("This path is newer and less validated than the standard stack; expect to read the logs of the wallet backend "
             "and issuer when something does not line up.",)),
        Template(
            "dc-api", "W3C Digital Credentials API on",
            "Explicitly enables the browser Digital Credentials API on the verifier, for testing presentation from a "
            "browser or a platform wallet.",
            {"schema_version": 1, "dc_api_enable": "true"},
            ("Set `dc_api_enable` to \"false\" to test the fallback without it.",)),
        Template(
            "interop", "Interop: registry, attestation and Digital Credentials API",
            "The three optional features together: credential types from registry.siros.org, wallet attestation and the "
            "Digital Credentials API. A good base for interoperability events and for checking a partner's wallet or verifier "
            "against the most demanding configuration.",
            {"schema_version": 1, "credential_registries": [REGISTRY_URL], "wallet_attestation": True, "dc_api_enable": "true"},
            ("Add the partner's issuer or verifier under `trusted_issuers` / `trusted_verifiers` before testing against them.",)),
        Template(
            "custom-wallet-backend", "Run my own wallet backend build",
            "The standard stack, with the wallet backend replaced by an image you name. It starts from the version this "
            "platform currently deploys, so change the tag (or the whole reference) to your own build. Needs permission to run "
            "custom images; ask an admin if this is refused.",
            {"schema_version": 1, "images": {"wallet-backend": pin}},
            ("Other components can be replaced the same way: add `\"vc-apigw\": \"ghcr.io/you/image:tag\"` and so on.",
             "Use a tag or digest that exists in a public registry; the platform does not check that it boots."),
            (CAP_CUSTOM_IMAGES,)),
    ]


def available_templates(capabilities, resources=None) -> List[dict]:
    """The templates a user holding `capabilities` can actually save, as plain dicts."""
    caps = set(capabilities)
    return [t.to_dict() for t in config_templates(resources) if set(t.requires) <= caps]


def get_template(template_id: str, capabilities, resources=None) -> Optional[dict]:
    return next((t for t in available_templates(capabilities, resources) if t["id"] == template_id), None)
