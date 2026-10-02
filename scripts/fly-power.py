#!/usr/bin/env python3
"""Stop or start a whole Fly environment: `make fly-stop ENV=<name>` / `make fly-start ENV=<name>`.

Stopping keeps the apps and the Mongo volume (only the volume is billed) and
starts nothing on its own; starting brings every component up in deploy order,
waiting for health where there is a check. Always the WHOLE environment: Fly
wakes a stopped machine on public traffic but never for internal 6PN calls, so a
single stopped component would leave the rest of the stack failing against it.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fly_common  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.lifecycle import start_instance, stop_instance  # noqa: E402
from sirosid_core.naming import Naming  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["stop", "start"])
    parser.add_argument("--env", required=True)
    parser.add_argument("--org", default="", help="Fly organization (default sirosfoundation)")
    parser.add_argument("--app-prefix", default="sirosid")
    args = parser.parse_args()
    fly = FlyClient(args.org) if args.org else fly_common._client
    naming = Naming(args.env, app_prefix=args.app_prefix)
    report = (stop_instance if args.action == "stop" else start_instance)(fly, naming, progress=print)
    for app, err in report.failed:
        print(f"FAILED {args.action} {app}: {err}", file=sys.stderr)
    if not report.changed and not report.failed:
        raise SystemExit(f"environment '{args.env}' has no apps (nothing to {args.action})")
    print(f"environment '{args.env}': {args.action} done for {len(report.changed)} app(s)"
          + (f", {len(report.failed)} failed" if report.failed else ""))
    raise SystemExit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
