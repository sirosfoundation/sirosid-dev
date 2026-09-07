#!/usr/bin/env python3
"""Tear down a named Fly.io environment for sirosid-dev: `make fly-down ENV=<name>`.

Destroys all Fly apps for the environment (see scripts/fly_common.py's
COMPONENTS table) and removes the local rendered-config directory. Order
doesn't matter for teardown (unlike fly-up.py) - Fly apps don't fail to
destroy just because another app was still calling them.

Destroying an app destroys its volumes, so a plain fly-down deletes the
environment's Mongo data too - a teardown is a teardown. `--keep-data`
(`make fly-down ENV=x KEEP_DATA=yes`) leaves the storage apps
(fly_common.STORAGE_APPS) in place with their machines stopped, so only the
volume is billed and the next `make fly-up ENV=x` finds the data again. The
local rendered-config directory is kept in that case too: it caches the
Mongo root password the volume's data was initialised with.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fly_common import (  # noqa: E402
    COMPONENTS, CONFORMANCE_COMPONENTS, STORAGE_APPS, app_exists, app_name, destroy_app, revoke_tokens,
    stop_machines,
)

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--keep-data", action="store_true",
                        help="keep the Mongo apps and their volumes (machines stopped) for the next fly-up")
    args = parser.parse_args()

    # Always attempt the conformance apps too, regardless of whether
    # --conformance was used at fly-up time - destroy_app() already tolerates
    # "does not exist" for every other component, so there's no need to track
    # whether this particular environment ever had them.
    for comp in COMPONENTS + CONFORMANCE_COMPONENTS:
        app = app_name(args.env, comp["name"])
        if args.keep_data and comp["name"] in STORAGE_APPS:
            if app_exists(app):
                print(f"--- keeping {app} (KEEP_DATA) - stopping its machine ---")
                stop_machines(app)
            continue
        if comp["name"] != "env-admin" and app_exists(app):
            # env-admin's per-consumer deploy tokens die with the consumer apps;
            # revoke them anyway so `fly tokens list` does not accumulate ghosts.
            revoke_tokens(app)
        print(f"--- destroying {app} ---")
        destroy_app(app)

    out_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}"
    if out_dir.exists() and not args.keep_data:
        shutil.rmtree(out_dir)
        print(f"removed {out_dir}")
    elif out_dir.exists():
        print(f"kept {out_dir} (holds the Mongo root password the kept volume needs)")

    if args.keep_data:
        print(f"environment '{args.env}' torn down; Mongo data kept - `make fly-up ENV={args.env}` reattaches it, "
              f"`make fly-storage-clear ENV={args.env}` deletes it")
    else:
        print(f"environment '{args.env}' torn down")


if __name__ == "__main__":
    main()
