"""Semantic (parsed-YAML, not textual) diffing of vc service configs.

Shared by scripts/vc-config-parity.py and its --capture mode. Kept as its own
importable module (no hyphen in the name) so both the parity tool and any
future test can use it without the importlib.util dance render-helm-config.py
needs (see fly-up.py's comment on that).

The unit of comparison is one service's fully-resolved config file - the exact
YAML that gets mounted at /config.yaml - not the chart's values or the legacy
fixture. That's deliberate: it's the only representation both the old
hand-maintained path and the new chart-rendered path actually agree on, and
it's what the vc binaries themselves parse (pkg/configuration/config.go reads
exactly one file, named by VC_CONFIG_YAML).
"""

# The four vc services, and which top-level section of the legacy single-file
# config belongs to each. Every service also gets `common` - that's how the
# chart already renders them (templates/04-issuer.yaml emits three ConfigMaps,
# 04-verifier.yaml one, each carrying `common` plus its own section), and how
# vc's own model.Cfg is structured (Common + APIGW/Issuer/Verifier/Registry).
SERVICE_SECTIONS = {
    "vc-apigw": "apigw",
    "vc-issuer": "issuer",
    "vc-verifier": "verifier",
    "vc-registry": "registry",
}


def split_legacy_config(cfg: dict) -> dict:
    """Split one legacy fixtures/vc-config.yaml-shaped dict into the per-service
    configs the chart renders, so the two can be compared at all.

    sirosid-dev historically mounted ONE config file into all four vc services;
    the chart renders four. The split is what makes them comparable - and is
    itself the improvement: an IMAGES= override on one service can no longer
    crash-loop a sibling that shares its config file (the constraint
    environments/gdc.yaml documents at length).

    A stray top-level key that model.Cfg has no field for (the legacy fixture
    has a top-level `kafka:` - the real one is `common.kafka`) is dropped here
    rather than carried into every service, since the loader ignores it anyway.
    """
    common = cfg.get("common") or {}
    out = {}
    for service, section in SERVICE_SECTIONS.items():
        if section not in cfg:
            continue
        out[service] = {"common": common, section: cfg[section]}
    return out


def _fmt(path: list) -> str:
    """Render a path as dotted notation, bracketing list indices and any key
    that itself contains a dot (VCT URLs and preset labels both do)."""
    parts = []
    for p in path:
        if isinstance(p, int):
            parts.append(f"[{p}]")
        elif "." in str(p) or " " in str(p):
            parts.append(f"[{p!r}]")
        else:
            parts.append(("." if parts else "") + str(p))
    return "".join(parts).lstrip(".")


def diff(old, new, path=None) -> list:
    """Recursively diff two parsed-YAML values.

    Returns a list of {path, kind, old, new} with kind in
    added/removed/changed. Lists are compared element-wise by index, then by
    length - good enough here because every list in this schema is either
    order-significant (priority, grant_types) or short enough to read.
    """
    path = path or []
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for key in sorted(set(old) | set(new), key=str):
            if key not in old:
                out.append({"path": _fmt(path + [key]), "kind": "added", "old": None, "new": new[key]})
            elif key not in new:
                out.append({"path": _fmt(path + [key]), "kind": "removed", "old": old[key], "new": None})
            else:
                out += diff(old[key], new[key], path + [key])
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for i in range(max(len(old), len(new))):
            if i >= len(old):
                out.append({"path": _fmt(path + [i]), "kind": "added", "old": None, "new": new[i]})
            elif i >= len(new):
                out.append({"path": _fmt(path + [i]), "kind": "removed", "old": old[i], "new": None})
            else:
                out += diff(old[i], new[i], path + [i])
        return out
    if old != new:
        return [{"path": _fmt(path), "kind": "changed", "old": old, "new": new}]
    return []


def is_accepted(finding: dict, accepted: list) -> dict:
    """Match a finding against the accepted-differences allow-list.

    An entry matches if its `path` equals the finding's path or is a prefix of
    it at a path-component boundary - so one entry can cover a whole subtree
    (e.g. `common.mongo` covering `common.mongo.uri`) without also matching an
    unrelated key that merely shares a textual prefix (`common.mongodb`).
    Returns the matching entry, or None.
    """
    for entry in accepted:
        want = entry.get("path", "")
        got = finding["path"]
        if got == want or got.startswith(want + ".") or got.startswith(want + "["):
            if "kind" in entry and entry["kind"] != finding["kind"]:
                continue
            return entry
    return None
