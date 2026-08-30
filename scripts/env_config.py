#!/usr/bin/env python3
"""Persisted, machine-readable per-environment defaults for `make fly-up`.

Every `make fly-up ENV=<name>` flag (IMAGES=, TRUSTED_ISSUERS=,
TRUSTED_VERIFIERS=, TRUSTED_VERIFIER_ROOTS=, ZK_CIRCUITS_SOURCES=,
CONFORMANCE=, WALLET_ATTESTATION=) is otherwise purely ephemeral - nothing
records what a given named environment is actually running, so a later
redeploy that forgets to repeat a flag silently drops it (e.g. a
TRUSTED_ISSUERS set on one run vanishes on the next unless retyped), and
overriding just one component's image with IMAGES= while forgetting a
sibling that shares the same config file can crash-loop the one left on an
older binary if the config's shape changed underneath it.

`environments/<name>.yaml` closes that gap for a *named, durable* test
environment (gdc, mdoc-test, ...) that's meant to be redeployed the same
way every time, by anyone. It is NOT for personal scratch environments
(alice/bob) - those have no file and behave exactly as before, CLI flags
only.

Precedence, matching scripts/android_apps.py's own layered-sources
convention (later adds to, never replaces, earlier ones) for the list-typed
fields (trusted_issuers/trusted_verifiers/trusted_verifier_roots/
zk_circuits_sources/android_apps): file entries are loaded first, then
whatever the CLI passes is appended on top - so a one-off extra flag never
requires editing the file, and the file's own list is never silently
dropped. For the dict-typed `images` field, CLI-provided component=image
pairs override the file's entry for that same component (last-one-wins,
same as a plain dict update) - the file is the "normal" pin, CLI is for a
genuine one-off "just this once" test build. For the two boolean fields
(conformance/wallet_attestation), the CLI flag ORs on top of the file's
value - there is no way to *disable* a file-enabled flag from the CLI,
since neither has a negating flag today; edit the file directly for that.

Schema (all keys optional):
    values: {...}                  # free-form Helm values, see below
    images: {component: image_ref}
    trusted_issuers: [url, ...]
    trusted_verifiers: [identity, ...]
    trusted_verifier_roots: [path, ...]           # relative to sirosid-dev root
    zk_circuits_sources: [url, ...]
    android_apps: ["package=fingerprint", ...]
    conformance: bool
    wallet_attestation: bool
    rical_provider_url: url        # RICAL (ISO 18013-5 2nd ed. Annex F) reader-trust list
    rical_root_cert: path          # relative to sirosid-dev root - PEM signer of the RICAL above
    dc_api_enable: "true" | "false"   # verifier.digital_credentials.enable override; "" (default) leaves
                                      # fixtures/vc-config.yaml's own value (true) untouched

For the scalar fields (the two RICAL ones plus dc_api_enable), a CLI value
overrides the file's (last-one-wins, same as `images`) rather than merging -
there's exactly one value per environment, unlike the list-typed fields
above.

`values:` is an arbitrary siros-id-stack values tree, deep-merged LAST by
render-helm-config.py - after values-base.yaml, the target's own
values-dev/values-fly.yaml, and the generated per-run overlay. It is not
validated here; `helm template` is the validator. Everything the chart can
express is reachable through it, including each service's `extraConfig`
escape hatch for fields the chart doesn't model - which is the point: adding
a one-off override for an environment should not need a code change in five
files, as adding `dc_api_enable` did.

The typed keys above are sugar for the common cases and keep working; where
both set the same thing, `values:` wins, since it is merged last. Prefer the
typed key when one exists - it is validated, and it is what `make env-show`
prints.

Applies to both targets: `make up ENV=<name>` layers the same file as
`make fly-up ENV=<name>`. Keys that only make sense for one of them (image
pins for Fly apps, say) are simply ignored by the other.

Run directly to pretty-print what a name resolves to (for Makefile's
`make env-show ENV=<name>` / debugging):
    python3 scripts/env_config.py --env gdc
"""
import argparse
import json
import sys
from pathlib import Path

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent

_LIST_KEYS = ("trusted_issuers", "trusted_verifiers", "trusted_verifier_roots", "zk_circuits_sources",
              "android_apps")
_BOOL_KEYS = ("conformance", "wallet_attestation")
_STR_KEYS = ("rical_provider_url", "rical_root_cert", "dc_api_enable")
_KNOWN_KEYS = frozenset(_LIST_KEYS + _BOOL_KEYS + _STR_KEYS + ("images", "values"))


def config_path(env_name: str, root: Path = None) -> Path:
    root = root or SIROSID_DEV_ROOT
    return root / "environments" / f"{env_name}.yaml"


def load_environment_config(env_name: str, root: Path = None) -> dict:
    """Returns a fully-populated dict (empty list/dict/False defaults for
    every known key) whether or not environments/<env_name>.yaml exists -
    callers never need to guess which keys are present."""
    root = root or SIROSID_DEV_ROOT
    result = {"images": {}, "values": {}, **{k: [] for k in _LIST_KEYS},
              **{k: False for k in _BOOL_KEYS}, **{k: "" for k in _STR_KEYS}}

    path = config_path(env_name, root)
    if not path.exists():
        return result

    import yaml  # lazy: only needed when a file actually exists
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a YAML mapping at the top level, got {type(raw).__name__}")

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise SystemExit(f"{path}: unknown key(s) {sorted(unknown)} - see scripts/env_config.py's "
                          "module doc for the supported schema")

    images = raw.get("images") or {}
    if not isinstance(images, dict):
        raise SystemExit(f"{path}: 'images' must be a mapping of component: image_ref")
    result["images"] = {str(k): str(v) for k, v in images.items()}

    for key in _LIST_KEYS:
        value = raw.get(key) or []
        if not isinstance(value, list):
            raise SystemExit(f"{path}: '{key}' must be a list")
        result[key] = [str(v) for v in value]

    for key in _BOOL_KEYS:
        if key in raw:
            result[key] = bool(raw[key])

    for key in _STR_KEYS:
        if key in raw:
            result[key] = str(raw[key])

    values = raw.get("values") or {}
    if not isinstance(values, dict):
        raise SystemExit(f"{path}: 'values' must be a mapping (a Helm values tree)")
    result["values"] = values

    return result


def merge_list(file_values: list, cli_values: list) -> list:
    """File entries first, then CLI-provided ones appended, de-duplicated
    (first occurrence wins position) - same shape as android_apps.py's own
    seen-set merge."""
    seen = set()
    out = []
    for v in list(file_values) + list(cli_values):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def merge_images(file_images: dict, cli_images: dict) -> dict:
    """CLI-provided component=image pairs override the file's pin for that
    component; every other file-pinned component is left as-is."""
    merged = dict(file_images)
    merged.update(cli_images)
    return merged


def _main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True)
    args = parser.parse_args()

    path = config_path(args.env)
    cfg = load_environment_config(args.env)
    if not path.exists():
        print(f"No persisted config at {path} - '{args.env}' uses CLI flags/defaults only.", file=sys.stderr)
    else:
        print(f"=== {path} ===", file=sys.stderr)
    print(json.dumps(cfg, indent=2, sort_keys=True))


if __name__ == "__main__":
    _main()
