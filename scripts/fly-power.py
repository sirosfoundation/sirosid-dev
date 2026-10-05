#!/usr/bin/env python3
"""Stop or start a whole Fly environment: `make fly-stop ENV=<name>` / `make fly-start ENV=<name>`.

Stopping keeps the apps and the Mongo volume (only the volume is billed) and
starts nothing on its own; starting brings every component up in deploy order,
waiting for health where there is a check. Always the WHOLE environment: Fly
wakes a stopped machine on public traffic but never for internal 6PN calls, so a
single stopped component would leave the rest of the stack failing against it.

With --single-machine (an instance deployed with fly-up --single-machine) each
action is one Machines API call on the one machine, and `reset` is available:
drop every Mongo database in place, restart the machine, register the issuer and
verifier again (it needs this checkout's state for the instance: adminToken).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fly_common  # noqa: E402
from sirosid_core.fly import FlyClient  # noqa: E402
from sirosid_core.lifecycle import (naming_from_machine, reset_single_machine, start_instance,  # noqa: E402
                                    stop_instance)
from sirosid_core.naming import Naming  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def _register_again(naming, args):
    import bootstrap
    from sirosid_core.deploy import RegistrationError, register_vc_services
    token_file = Path(args.rendered_root or SIROSID_DEV_ROOT / "fixtures" / "rendered") / f"fly-{args.env}" / "adminToken"
    if not token_file.exists():
        print(f"not re-registering the issuer/verifier: no {token_file} (deployed from another checkout?)",
              file=sys.stderr)
        return

    def register(admin_url, admin_token, issuer_url, verifier_url):
        try:
            return bootstrap.register(admin_url, admin_token, issuer_url, verifier_url)
        except bootstrap.BootstrapError as e:
            raise RegistrationError(str(e)) from e
    register_vc_services(naming, token_file.read_text().strip(), register, print,
                         admin_url=f"https://{naming.machine_app()}.fly.dev")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["stop", "start", "reset"])
    parser.add_argument("--env", required=True)
    parser.add_argument("--org", default="", help="Fly organization (default sirosfoundation)")
    parser.add_argument("--app-prefix", default="sirosid")
    parser.add_argument("--single-machine", action="store_true",
                        help="the environment was deployed with fly-up --single-machine")
    parser.add_argument("--rendered-root", default="", help="where fly-up kept fly-<env>/ (reset re-registers from it)")
    args = parser.parse_args()
    fly = FlyClient(args.org) if args.org else fly_common._client
    naming = Naming(args.env, app_prefix=args.app_prefix, layout="single-machine" if args.single_machine else "apps")
    machines = fly_common.machines_client() if args.single_machine else None
    if args.action == "reset":
        if not args.single_machine:
            raise SystemExit("reset is the single-machine layout's; a default-layout environment clears its data "
                             "through env-admin: make fly-storage-clear ENV=<name>")
        naming = naming_from_machine(machines, naming)   # the deployed host pattern
        report = reset_single_machine(fly, machines, naming, progress=print)
        if report.ok and report.changed:
            _register_again(naming, args)
    elif args.action == "stop":
        report = stop_instance(fly, naming, progress=print, machines=machines)
    else:
        report = start_instance(fly, naming, progress=print, machines=machines)
    for app, err in report.failed:
        print(f"FAILED {args.action} {app}: {err}", file=sys.stderr)
    if not report.changed and not report.failed:
        raise SystemExit(f"environment '{args.env}' has no apps (nothing to {args.action})")
    print(f"environment '{args.env}': {args.action} done for {len(report.changed)} app(s)"
          + (f", {len(report.failed)} failed" if report.failed else ""))
    raise SystemExit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
