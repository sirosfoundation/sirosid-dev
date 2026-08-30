#!/usr/bin/env python3
"""Compare the chart-rendered vc configs against a checked-in golden set.

`make vc-config-parity`.

This exists for two jobs, in this order:

1. As the gate for deleting fixtures/vc-config.yaml and its five
   mechanically-patched variants. The goldens were seeded from what those
   files actually produced, so a clean run means the chart renders the same
   thing - and every remaining difference had to be looked at and either
   fixed or written down in accepted-diffs.yaml with a reason.

2. Permanently after that, as a regression check on the chart itself.
   ../siros-id-stack is fast-forwarded by `make setup` and released on its own
   cadence, so an upstream change can silently drop a field this repo depends
   on. Nothing else would notice until a credential fails to issue.

A finding is a semantic difference between two parsed configs - key ordering
and formatting are irrelevant. Run with --update to re-seed the goldens after
a change that is meant to move them; read the diff before you do.
"""
import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vc_config_diff import diff, is_accepted, split_legacy_config  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = SIROSID_DEV_ROOT / "fixtures" / "vc-config-golden"
ACCEPTED = GOLDEN_DIR / "accepted-diffs.yaml"

# The contexts worth checking. Each is a full render of the chart, differing
# only in which values files are layered on - which is the whole point: these
# used to be five separately-patched copies of one hand-written file.
CONTEXTS = {
    # dc_api_enable mirrors the Makefile's own `DC_API ?= no` default, so this
    # checks what a plain `make up VC=yes` actually renders.
    "compose": {"target": "compose", "dc_api_enable": "false"},
    "fly-gdc": {"target": "fly", "env": "gdc"},
}


def render_context(name: str, spec: dict, chart_dir: Path, out_root: Path) -> dict:
    """Render one context and return {service: parsed config}."""
    spec_file = importlib.util.spec_from_file_location(
        "render_helm_config", Path(__file__).resolve().parent / "render-helm-config.py")
    module = importlib.util.module_from_spec(spec_file)
    spec_file.loader.exec_module(module)

    out_dir = out_root / name
    out_dir.mkdir(parents=True, exist_ok=True)
    env = spec.get("env")
    env_values = {}
    if env:
        import env_config
        env_values = env_config.load_environment_config(env).get("values") or {}
    module.render(spec["target"], chart_dir, env=env, out_dir=out_dir,
                  secrets_dir=out_root / "secrets", env_values=env_values,
                  dc_api_enable=spec.get("dc_api_enable", ""),
                  # A real password only matters for a deploy; parity compares
                  # config shape, and the goldens redact it either way.
                  mongo_password="parity-check")
    base = out_dir / f"fly-{env}" if spec["target"] == "fly" else out_dir
    return {p.stem: yaml.safe_load(p.read_text())
            for p in sorted(base.glob("vc-*.yaml")) if not p.stem.endswith("-secrets")}


def redact(config):
    """Blank out values that legitimately differ run to run, so they don't
    swamp the real findings: generated secrets and the mongo password."""
    if isinstance(config, dict):
        return {k: ("<redacted>" if k in ("password", "client_secret", "subject_salt") else redact(v))
                for k, v in config.items()}
    if isinstance(config, list):
        return [redact(v) for v in config]
    if isinstance(config, str) and config.startswith("mongodb://") and "@" in config:
        return "mongodb://<redacted>@" + config.split("@", 1)[1]
    return config


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT.parent / "siros-id-stack"))
    ap.add_argument("--context", action="append", choices=sorted(CONTEXTS),
                    help="only check these (default: all)")
    ap.add_argument("--update", action="store_true",
                    help="re-seed the goldens from the current render")
    args = ap.parse_args()

    chart_dir = Path(args.chart_dir)
    if not chart_dir.is_dir():
        raise SystemExit(f"chart not found at {chart_dir} - run `make setup`")

    accepted_all = yaml.safe_load(ACCEPTED.read_text()) if ACCEPTED.exists() else {}
    out_root = Path(subprocess.run(["mktemp", "-d"], capture_output=True, text=True).stdout.strip())
    failures = 0
    try:
        for name in (args.context or sorted(CONTEXTS)):
            rendered = {svc: redact(cfg)
                        for svc, cfg in render_context(name, CONTEXTS[name], chart_dir, out_root).items()}
            golden_ctx = GOLDEN_DIR / name
            if args.update:
                if golden_ctx.exists():
                    shutil.rmtree(golden_ctx)
                golden_ctx.mkdir(parents=True)
                for svc, cfg in rendered.items():
                    (golden_ctx / f"{svc}.yaml").write_text(yaml.dump(cfg, sort_keys=False))
                print(f"{name}: updated {len(rendered)} golden files")
                continue

            # Each context's list starts with a `*shared` anchor, which YAML
            # splices in as one nested list rather than merging - flatten it.
            accepted = []
            for entry in accepted_all.get(name) or []:
                accepted.extend(entry if isinstance(entry, list) else [entry])
            print(f"\n=== {name} ===")
            for svc, cfg in sorted(rendered.items()):
                path = golden_ctx / f"{svc}.yaml"
                if not path.exists():
                    print(f"  {svc}: NO GOLDEN (run --update to seed)")
                    failures += 1
                    continue
                findings = diff(yaml.safe_load(path.read_text()), cfg)
                unexpected = [f for f in findings if not is_accepted(f, accepted)]
                ok = len(findings) - len(unexpected)
                if not unexpected:
                    print(f"  {svc}: clean" + (f" ({ok} accepted)" if ok else ""))
                    continue
                failures += len(unexpected)
                print(f"  {svc}: {len(unexpected)} unexpected" + (f", {ok} accepted" if ok else ""))
                for f in unexpected:
                    print(f"      {f['kind']:8} {f['path']}")
                    print(f"          golden: {f['old']!r:.100}")
                    print(f"          now:    {f['new']!r:.100}")
    finally:
        shutil.rmtree(out_root, ignore_errors=True)

    if args.update:
        return
    print()
    if failures:
        raise SystemExit(f"{failures} unexpected difference(s) - fix them, or add an entry with a "
                         f"reason to {ACCEPTED.relative_to(SIROSID_DEV_ROOT)}")
    print("all contexts match their goldens")


if __name__ == "__main__":
    main()
