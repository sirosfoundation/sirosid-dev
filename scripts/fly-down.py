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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fly_common  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.lifecycle import destroy_instance  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True)
    parser.add_argument("--keep-data", action="store_true",
                        help="keep the Mongo apps and their volumes (machines stopped) for the next fly-up")
    parser.add_argument("--org", default="",
                        help="Fly organization the environment was deployed in (fly-up --org); default sirosfoundation")
    parser.add_argument("--app-prefix", default="sirosid",
                        help="app-name prefix the environment was deployed with (fly-up --app-prefix)")
    args = parser.parse_args()

    fly = FlyClient(args.org) if args.org else fly_common._client
    report = destroy_instance(fly, Naming(args.env, app_prefix=args.app_prefix),
                              keep_data=args.keep_data, progress=print)
    for app, err in report.failed:
        print(f"FAILED to tear down {app}: {err}", file=sys.stderr)

    out_dir = SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}"
    if report.failed:
        raise SystemExit(f"environment '{args.env}' only partly torn down ({len(report.failed)} app(s) failed); "
                         f"the local working directory was left in place. Re-run `make fly-down ENV={args.env}`.")
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
