#!/usr/bin/env python3
"""Tear down a named Fly.io environment for sirosid-dev: `make fly-down ENV=<name>`.

Destroys all 9 Fly apps for the environment (see scripts/fly_common.py's
COMPONENTS table) and removes the local rendered-config directory. Order
doesn't matter for teardown (unlike fly-up.py) - Fly apps don't fail to
destroy just because another app was still calling them.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fly_common import COMPONENTS, app_name, destroy_app  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    args = parser.parse_args()

    for comp in COMPONENTS:
        app = app_name(args.env, comp["name"])
        print(f"--- destroying {app} ---")
        destroy_app(app)

    out_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
        print(f"removed {out_dir}")

    print(f"environment '{args.env}' torn down")


if __name__ == "__main__":
    main()
