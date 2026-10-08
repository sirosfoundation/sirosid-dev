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


def destroy_instance(fly: FlyClient, naming: Naming, keep_data: bool = False, progress=None,
                     machines=None) -> DestroyReport:
    """Destroy every app of the instance `naming` describes.

    Order does not matter for teardown (unlike deploy): Fly apps do not fail to
    destroy because another app was still calling them. The conformance apps are
    always attempted, whether or not the instance ever had them - destroying an
    app that does not exist is already a no-op.

    Destroying an app destroys its volumes, so without keep_data this deletes the
    instance's Mongo data. With keep_data the storage apps stay, machines stopped,
    so only the volume is billed and a later deploy finds the data again.

    A failure on one app is recorded and the rest are still attempted.

    A single-machine instance (naming.layout) is one app: destroying it is one
    call, and keep_data stops its machine instead. `machines` (a MachinesClient)
    is optional for this; flyctl is used without one.
    """
    say = progress or (lambda msg: None)
    if naming.single_machine:
        return _destroy_single_machine(fly, naming, keep_data, say, machines)
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


def stop_instance(fly: FlyClient, naming: Naming, progress=None, machines=None) -> PowerReport:
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
    if naming.single_machine:
        return _power_single_machine(fly, naming, "stop", say, machines)
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


def start_instance(fly: FlyClient, naming: Naming, progress=None, machines=None, ready_timeout: float = 900) -> PowerReport:
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
    if naming.single_machine:
        return _power_single_machine(fly, naming, "start", say, machines, ready_timeout)
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


# ---- the single-machine layout (naming.layout == "single-machine") ------------
#
# One app, one machine: every operation is one call. `machines` is a
# MachinesClient; without one, flyctl's machine commands do the same job (but
# cannot wait for container health).

# Databases a reset never touches (Mongo's own).
SYSTEM_DATABASES = ("admin", "local", "config")


def detect_layout(fly: FlyClient, naming: Naming) -> Naming:
    """The naming an EXISTING instance was deployed with, for a caller (the CLI)
    that only knows the env and prefix: a single-machine instance's one app is
    `<prefix>-<env>`, a name the apps layout never uses."""
    from dataclasses import replace
    if not naming.single_machine and fly.app_exists(naming.machine_app()):
        return replace(naming, layout="single-machine")
    return naming


def naming_from_machine(machines, naming: Naming) -> Naming:
    """The single-machine instance's Naming as deployed: the host pattern is in
    its machine's metadata, so a caller that knows only env + prefix (the CLI)
    registers the issuer under the right public URL after a reset."""
    from dataclasses import replace
    for m in machines.list_machines(naming.machine_app()):
        meta = (m.get("config") or {}).get("metadata") or {}
        if meta.get("sirosid_host_pattern"):
            return replace(naming, host_pattern=meta["sirosid_host_pattern"])
    return naming


def _machine_ids(fly: FlyClient, app: str, machines) -> list:
    ms = machines.list_machines(app) if machines is not None else fly.list_machines(app)
    return [m for m in ms if m.get("state") != "destroyed"]


def _destroy_single_machine(fly, naming, keep_data, say, machines) -> DestroyReport:
    report = DestroyReport()
    app = naming.machine_app()
    try:
        if keep_data:
            if fly.app_exists(app):
                say(f"--- keeping {app} (KEEP_DATA) - stopping its machine ---")
                _power_single_machine(fly, naming, "stop", say, machines)
                report.kept.append(app)
            else:
                report.absent.append(app)
            return report
        say(f"--- destroying {app} (one app: machine, volume, secrets and bundle image go with it) ---")
        (report.destroyed if fly.destroy_app(app) else report.absent).append(app)
    except Exception as e:  # FlyError / MachinesError: report, never raise from a reaper
        report.failed.append((app, str(e)))
    return report


def _power_single_machine(fly, naming, action, say, machines, ready_timeout: float = 900) -> PowerReport:
    report = PowerReport()
    app = naming.machine_app()
    try:
        if not fly.app_exists(app):
            report.absent.append(app)
            return report
        for m in _machine_ids(fly, app, machines):
            say(f"--- {action} {app} machine {m['id']} ---")
            if action == "stop":
                # Cordon FIRST: behind a fly-replay edge the replay STARTS a stopped
                # machine even with autostart off (real Fly, 2026-10-05: event source
                # "proxy"), so "stopped" only stays stopped while the proxy may not
                # route to it. The edge's fallback then shows its 503 page.
                _cordon(fly, machines, app, m["id"], True)
                if m.get("state") not in ("stopped", "suspended"):
                    if machines is not None:
                        machines.stop_machine(app, m["id"])
                        machines.wait(app, m["id"], "stopped", timeout=120)
                    else:
                        fly.run("machine", "stop", m["id"], "-a", app)
            else:
                if m.get("state") != "started":
                    if machines is not None:
                        machines.start_machine(app, m["id"])
                    else:
                        fly.run("machine", "start", m["id"], "-a", app)
                try:
                    if machines is not None:
                        _wait_ready(machines, app, m["id"], say, ready_timeout)
                finally:
                    # Routable again once it is up (until then the edge says 503, not a
                    # half-started stack's 502s) - and also if waiting failed, which would
                    # otherwise leave a running instance unreachable.
                    _cordon(fly, machines, app, m["id"], False)
        report.changed.append(app)
    except Exception as e:
        report.failed.append((app, str(e)))
    return report


def _cordon(fly, machines, app, machine_id, on: bool):
    """Take the machine out of (on=True) or back into Fly's proxy routing. Idempotent;
    a cordon survives start, stop and a config update (real Fly)."""
    if machines is not None:
        (machines.cordon if on else machines.uncordon)(app, machine_id)
    else:
        fly.run("machine", "cordon" if on else "uncordon", machine_id, "-a", app)


def _wait_ready(machines, app, machine_id, say, timeout):
    from .singlemachine import readiness
    machines.wait(app, machine_id, "started", timeout=300)
    config = machines.get_machine(app, machine_id).get("config") or {}
    machines.wait_containers(app, machine_id, readiness(config), timeout=timeout, progress=say)


def reset_single_machine(fly: FlyClient, machines, naming: Naming, progress=None,
                         ready_timeout: float = 900) -> PowerReport:
    """Wipe a single-machine instance's data in place: drop every non-system
    Mongo database from inside the mongodb container, then restart the machine
    and wait until every container is healthy again.

    The restart is not optional (see CLAUDE.md, Storage): wallet-backend creates
    its default tenant and vc-apigw imports its bootstrap documents only at
    startup. Registering the issuer and verifier again is the caller's step
    (deploy.register_vc_services), because it needs the instance's admin token.
    Needs no state and no session: the password is read inside the container.
    """
    from .singlemachine import SECRETS_DIR
    say = progress or (lambda msg: None)
    report = PowerReport()
    app = naming.machine_app()
    keep = ",".join(f'"{d}"' for d in SYSTEM_DATABASES)
    script = (f'mongosh --quiet -u root -p "$(cat {SECRETS_DIR}/mongoRootPassword)" --authenticationDatabase admin '
              f"--eval 'db.adminCommand({{listDatabases:1}}).databases.map(d=>d.name)"
              f".filter(n=>![{keep}].includes(n))"
              f'.forEach(n=>{{db.getSiblingDB(n).dropDatabase(); print("dropped "+n)}})\'')
    try:
        if not fly.app_exists(app):
            report.absent.append(app)
            return report
        for m in _machine_ids(fly, app, machines):
            if m.get("state") != "started":
                machines.start_machine(app, m["id"])
                _wait_ready(machines, app, m["id"], say, ready_timeout)
            say(f"--- dropping Mongo data in {app} ---")
            out = machines.exec(app, m["id"], ["sh", "-c", script], container="mongodb", timeout=60)
            if out.get("exit_code") != 0:
                raise RuntimeError(f"dropping the databases failed (exit {out.get('exit_code')}): "
                                   f"{(out.get('stderr') or '').strip()[:300]}")
            for line in (out.get("stdout") or "").splitlines():
                say(f"    {line}")
            say(f"--- restarting {app} so every consumer starts on empty data ---")
            # stop + start rather than restart: a wait right after a restart can
            # still see the containers' previous "healthy".
            machines.stop_machine(app, m["id"])
            machines.wait(app, m["id"], "stopped", timeout=120)
            machines.start_machine(app, m["id"])
            try:
                _wait_ready(machines, app, m["id"], say, ready_timeout)
            finally:
                # A reset leaves the instance running and reachable, even one that
                # was stopped (so cordoned) before.
                machines.uncordon(app, m["id"])
        report.changed.append(app)
    except Exception as e:
        report.failed.append((app, str(e)))
    return report
