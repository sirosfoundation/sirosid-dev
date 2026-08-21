#!/usr/bin/env python3
"""Inline a VCTM's remote image references as data: URIs.

Credential type metadata references its logo and SVG render template by URL.
The wallet does not merely <img src> those - it fetch()es them, to substitute
claim values into the SVG template - so a cross-origin URL only works if that
host sends CORS headers. demo-issuer.wwwallet.org, which the fixtures under
fixtures/vc-metadata point at, does NOT (verified: 200 with no
Access-Control-Allow-Origin), so the browser blocks the fetch and the
credential renders as a broken image.

registry.siros.org already solves this for the types it publishes: it serves
each VCTM with the images inlined as data:image/svg+xml;base64 URIs, keeping
the original "uri#integrity" hash (which still describes the decoded bytes).
This script applies the same transformation locally, for the one type the
public registry does not carry - see the Makefile's REGISTRY_SOURCE_LOCAL_
OVERRIDES.

Failure is non-fatal by design: on a network error the reference is left as
it was and a warning printed, so `make up` still works offline. The
credential then renders exactly as badly as it would have without this
script - no worse.
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT = 20
# Guard against a pathological download being inlined into config; the real
# templates are ~130KB.
MAX_BYTES = 5 * 1024 * 1024


def _inline(ref: dict, what: str) -> bool:
    """Replace ref["uri"] with a data: URI. Returns True if changed."""
    uri = ref.get("uri", "")
    if not uri.startswith(("http://", "https://")):
        return False  # already inlined, or nothing to do
    try:
        with urllib.request.urlopen(uri, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                raise urllib.error.URLError(f"HTTP {resp.status}")
            body = resp.read(MAX_BYTES + 1)
            content_type = resp.headers.get("Content-Type", "image/svg+xml").split(";")[0].strip()
    except Exception as err:  # noqa: BLE001 - any failure must stay non-fatal
        print(f"  warning: leaving {what} as a remote URL ({err}): {uri}", file=sys.stderr)
        return False

    if len(body) > MAX_BYTES:
        print(f"  warning: {what} exceeds {MAX_BYTES} bytes, left as a remote URL: {uri}",
              file=sys.stderr)
        return False

    ref["uri"] = f"data:{content_type};base64," + base64.b64encode(body).decode()
    # "uri#integrity" is deliberately preserved: it describes the resource
    # bytes, which inlining does not change. registry.siros.org keeps it too.
    return True


def main():
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} <input.json> <output.json>", file=sys.stderr)
        return 2

    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    doc = json.loads(src.read_text())

    changed = 0
    for display in doc.get("display", []) or []:
        rendering = display.get("rendering") or {}
        logo = (rendering.get("simple") or {}).get("logo")
        if isinstance(logo, dict) and _inline(logo, "simple.logo"):
            changed += 1
        for i, tpl in enumerate(rendering.get("svg_templates") or []):
            if isinstance(tpl, dict) and _inline(tpl, f"svg_templates[{i}]"):
                changed += 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {dst.name} ({doc.get('vct', '?')}): {changed} image reference(s) inlined")
    return 0


if __name__ == "__main__":
    sys.exit(main())
