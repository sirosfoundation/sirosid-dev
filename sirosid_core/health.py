"""How an instance's components are doing, right now, from Fly's own view.

A probe, not a deploy step: it is called whenever someone looks at an instance (the
console's status panel polls it, an agent waits on it), so it is cheap and bounded and
NEVER raises. A Fly hiccup is an answer (`error`), not an exception, and the report
carries only names and states - never a machine's config, env or files, which is where
an instance's non-secret-but-private configuration lives.

Both layouts go through the Machines REST API (the same org credential flyctl uses):
  single-machine  one app, one machine: GET the machine, read containers[] - per-container
                  state lives only there (see MachinesClient.container_states).
  apps            one app per component: list each component's machines, in parallel,
                  and read their state and health checks. An app that does not exist is a
                  component this instance does not have (conformance, env-admin), not an error.
"""
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import List

from .components import component_names
from .machines import MachinesError
from .naming import Naming

DEFAULT_TIMEOUT = 5.0      # one HTTP call
DEFAULT_BUDGET = 8.0       # the whole probe


@dataclass
class HealthReport:
    components: List[dict] = field(default_factory=list)   # [{name, state, healthy, detail}]
    error: str = ""

    @property
    def healthy(self) -> bool:
        return bool(self.components) and not self.error and all(c["healthy"] for c in self.components)


def _entry(name, state, healthy, detail=""):
    return {"name": name, "state": state or "unknown", "healthy": bool(healthy), "detail": detail}


def instance_health(machines, naming: Naming, timeout: float = DEFAULT_TIMEOUT,
                    budget: float = DEFAULT_BUDGET) -> HealthReport:
    try:
        if naming.single_machine:
            return _single_machine(machines, naming, timeout)
        return _apps(machines, naming, timeout, budget)
    except Exception as e:                       # noqa: BLE001 - a probe never raises
        return HealthReport(error=_short(e))


def _short(e: Exception) -> str:
    text = str(e).splitlines()[0] if str(e) else type(e).__name__
    return f"could not reach Fly: {text}"[:200]


def _single_machine(machines, naming: Naming, timeout: float) -> HealthReport:
    from .singlemachine import readiness
    app = naming.machine_app()
    try:
        listed = [m for m in machines.list_machines(app, timeout=timeout, retry=False) if m.get("state") != "destroyed"]
    except MachinesError as e:
        if e.status == 404:
            return HealthReport(error="the instance has no app on Fly (it was never deployed, or was destroyed)")
        raise
    if not listed:
        return HealthReport(error="the instance's app has no machine")
    m = machines.get_machine(app, listed[0]["id"], timeout=timeout, retry=False)
    state = m.get("state", "")
    try:
        want = readiness(m.get("config") or {"containers": []})
    except (KeyError, TypeError):
        want = {}
    out = []
    for c in m.get("containers") or []:
        name, cstate = c.get("name", "?"), c.get("state", "")
        kind = want.get(name, "healthy")
        if state != "started":
            out.append(_entry(name, cstate, False, f"the machine is {state or 'unknown'}"))
        elif kind == "exited":
            done = cstate in ("stopped", "exited")
            out.append(_entry(name, cstate, done, "setup step: runs once at boot" if done else "setup step still running"))
        else:
            ok = cstate == "healthy" or (name not in want and cstate == "started")
            out.append(_entry(name, cstate, ok, "" if ok else "not healthy yet"))
    if not out:
        return HealthReport(components=[], error=f"the machine is {state or 'unknown'} and reports no containers")
    return HealthReport(components=out)


def _app_status(machines, naming: Naming, component: str, timeout: float):
    try:
        ms = [m for m in machines.list_machines(naming.app(component), timeout=timeout, retry=False)
              if m.get("state") != "destroyed"]
    except MachinesError as e:
        if e.status == 404:
            return None                               # this instance has no such component
        raise
    if not ms:
        return _entry(component, "no machine", False, "the app has no machine")
    states = sorted({m.get("state", "") for m in ms})
    checks = [c for m in ms for c in (m.get("checks") or [])]
    passing = [c for c in checks if c.get("status") == "passing"]
    started = states == ["started"]
    ok = started and len(passing) == len(checks)
    if not started:
        detail = "machine " + "/".join(s or "unknown" for s in states)
    elif checks:
        detail = f"checks {len(passing)}/{len(checks)} passing"
    else:
        detail = ""
    return _entry(component, states[0] if len(states) == 1 else "mixed", ok, detail)


def _apps(machines, naming: Naming, timeout: float, budget: float) -> HealthReport:
    names = component_names()
    deadline = time.monotonic() + budget
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="health")
    try:
        futures = {pool.submit(_app_status, machines, naming, n, timeout): n for n in names}
        done, pending = wait(futures, timeout=max(0.1, deadline - time.monotonic()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    by_name, errors = {}, []
    for f in done:
        try:
            r = f.result()
        except Exception as e:                    # noqa: BLE001
            errors.append(_short(e))
            continue
        if r is not None:
            by_name[futures[f]] = r
    error = ""
    if pending:
        error = f"Fly did not answer for {len(pending)} component(s) in time"
    elif errors:
        error = errors[0]
    comps = [by_name[n] for n in names if n in by_name]
    if not comps and not error:
        error = "the instance has no apps on Fly (it was never deployed, or was destroyed)"
    return HealthReport(components=comps, error=error)
