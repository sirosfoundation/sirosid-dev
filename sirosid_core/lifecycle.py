"""Instance lifecycle operations that act on Fly: tearing an instance down.

Written as functions of (FlyClient, Naming) so the CLI, a service's TTL reaper
and a test all call the same code. They report instead of printing or exiting:
a reaper that stops at the first app it cannot destroy leaves the rest billing,
so every app is attempted and the failures are returned.
"""
from dataclasses import dataclass, field

from .components import STORAGE_APPS, component_names
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
