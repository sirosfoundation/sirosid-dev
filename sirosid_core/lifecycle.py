"""Instance lifecycle operations that act on Fly: tearing an instance down.

Written as functions of (FlyClient, Naming) so the CLI, a service's TTL reaper
and a test all call the same code. They report instead of printing or exiting:
a reaper that stops at the first app it cannot destroy leaves the rest billing,
so every app is attempted and the failures are returned.
"""
from dataclasses import dataclass, field

from .components import CONFORMANCE_COMPONENTS, STORAGE_APPS, build_components, component_names
from .fly import FlyClient, FlyError
from .naming import Naming


@dataclass
class DestroyReport:
    destroyed: list = field(default_factory=list)   # apps that existed and were destroyed
    kept: list = field(default_factory=list)        # storage apps left (machines stopped) for keep_data
    absent: list = field(default_factory=list)      # apps there was nothing to destroy for
    failed: list = field(default_factory=list)      # (app, error message): the operator must look

    @property
    def ok(self) -> bool:
        return not self.failed


def destroy_instance(fly: FlyClient, naming: Naming, keep_data: bool = False, progress=None) -> DestroyReport:
    """Destroy every app of the instance `naming` describes.

    Order does not matter for teardown (unlike deploy): Fly apps do not fail to
    destroy because another app was still calling them. The conformance apps are
    always attempted, whether or not the instance ever had them - destroying an
    app that does not exist is already a no-op.

    Destroying an app destroys its volumes, so without keep_data this deletes the
    instance's Mongo data. With keep_data the storage apps stay, machines stopped,
    so only the volume is billed and a later deploy finds the data again.

    A failure on one app is recorded and the rest are still attempted.
    """
    say = progress or (lambda msg: None)
    report = DestroyReport()
    for name in component_names():
        app = naming.app(name)
        try:
            if keep_data and name in STORAGE_APPS:
                if fly.app_exists(app):
                    say(f"--- keeping {app} (KEEP_DATA) - stopping its machine ---")
                    fly.stop_machines(app)
                    report.kept.append(app)
                continue
            if name != "env-admin" and fly.app_exists(app):
                # env-admin's per-consumer deploy tokens die with the consumer
                # apps; revoke them anyway so `fly tokens list` does not
                # accumulate ghosts.
                fly.revoke_tokens(app)
            say(f"--- destroying {app} ---")
            (report.destroyed if fly.destroy_app(app) else report.absent).append(app)
        except FlyError as e:
            report.failed.append((app, str(e)))
    return report


@dataclass
class PowerReport:
    changed: list = field(default_factory=list)    # apps whose machines were stopped / started
    absent: list = field(default_factory=list)     # apps that do not exist
    failed: list = field(default_factory=list)     # (app, error message)

    @property
    def ok(self) -> bool:
        return not self.failed


def stop_instance(fly: FlyClient, naming: Naming, progress=None) -> PowerReport:
    """Stop every machine of the instance: the "zero" of scale-to-zero.

    The instance's apps and its Mongo volume are kept, so nothing is lost and only
    the volume is billed. This is done to the WHOLE instance, never one app: Fly
    starts a stopped machine on traffic through its public edge, but never for a
    direct 6PN call between sibling apps, so a stopped mongodb, vc-issuer, pdp or
    wallet-backend would stay stopped for ever while the frontend in front of it
    was awake (this was tried per-app and abandoned - see assets.write_fly_toml).

    Consumers go first and Mongo last, the reverse of deploy order, so nothing is
    left calling a store that is already gone. A failure on one app is recorded and
    the rest are still stopped.
    """
    say = progress or (lambda msg: None)
    report = PowerReport()
    for name in reversed(component_names()):
        app = naming.app(name)
        try:
            if not fly.app_exists(app):
                report.absent.append(app)
                continue
            say(f"--- stopping {app} ---")
            fly.stop_machines(app)
            report.changed.append(app)
        except FlyError as e:
            report.failed.append((app, str(e)))
    return report


def start_instance(fly: FlyClient, naming: Naming, progress=None) -> PowerReport:
    """Start every machine of a stopped instance, in deploy order, waiting for each
    component that has a health check before starting the next - mongodb must be
    answering before vc-registry connects to it, the issuer before the verifier
    calls it, and so on, exactly as in a deploy.

    Components that are already running are left alone, so it is safe to call on a
    running or half-awake instance (the state a stray request leaves behind when
    the machines can wake themselves). A component whose check never turns healthy
    does not stop the rest: it is reported by wait_for_checks and the instance
    comes up as far as it can.
    """
    say = progress or (lambda msg: None)
    report = PowerReport()
    comps = {c["name"]: c for c in build_components("", "") + CONFORMANCE_COMPONENTS}
    for name in component_names():
        app = naming.app(name)
        try:
            if not fly.app_exists(app):
                report.absent.append(app)
                continue
            say(f"--- starting {app} ---")
            fly.ensure_running(app)
            comp = comps[name]
            if comp.get("checks") or comp.get("internal_check"):
                fly.wait_for_checks(app)
            report.changed.append(app)
        except FlyError as e:
            report.failed.append((app, str(e)))
    return report
