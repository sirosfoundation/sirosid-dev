"""MachinesClient - the parts of Fly's Machines REST API a single-machine instance needs.

flyctl cannot create the machine a single-machine instance runs on (`fly deploy`
has no notion of a `containers` array), so this talks to the Machines API
directly: create / update / get / wait / stop / start / restart / destroy a
machine, exec in one of its containers, and set app secrets.

Standard library only (urllib), like the rest of sirosid_core. The HTTP layer is
an injectable `transport(method, url, headers, body, timeout) -> (status, bytes)`
so tests run against tests/fakemachines.py instead of Fly.

The token is used in exactly one place - the Authorization header of a request -
and never appears in a message, an exception or repr(). Request bodies are not
echoed in errors either: a secret's value is a request body, and the secrets
endpoint echoes the value back in its RESPONSE, so set_secret() reports neither.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.machines.dev/v1"


class MachinesError(RuntimeError):
    """A Machines API call failed. `status` is the HTTP status (None: no response)."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def urllib_transport(method, url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def authorization(token: str) -> str:
    """Fly tokens are sent as-is ("FlyV1 fm2_..."); a bare token is a Bearer one."""
    token = token.strip()
    return token if token.split(" ", 1)[0] in ("FlyV1", "Bearer") else f"Bearer {token}"


class MachinesClient:
    def __init__(self, token: str, base_url: str = API, transport=None, timeout: float = 60,
                 sleep=None, clock=None):
        if not token:
            raise MachinesError("the Machines API needs a token")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._transport = transport or urllib_transport
        self.timeout = timeout
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic

    def __repr__(self):
        return f"MachinesClient(base_url={self.base_url!r})"

    @property
    def token(self) -> str:
        """For a caller that must hand the SAME identity to another Fly endpoint
        (the image registry). Never log it."""
        return self._token

    # ---- plumbing -------------------------------------------------------------

    def _call(self, method, path, body=None, ok=(200, 201, 202, 204), quiet=False, timeout=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": authorization(self._token), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # A read is retried on a network error (a DNS hiccup must not fail a
        # 10-minute deploy or a reaper pass); a write is not - it may have landed.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                status, raw = self._transport(method, self.base_url + path, headers, data, timeout or self.timeout)
                break
            except OSError as e:
                if attempt + 1 == attempts:
                    raise MachinesError(f"{method} {path}: {type(e).__name__}: {e}") from None
                self._sleep(2 * (attempt + 1))
        if status not in ok:
            detail = "" if quiet else ": " + (raw or b"").decode("utf-8", "replace").strip()[:300]
            raise MachinesError(f"{method} {path}: HTTP {status}{detail}", status)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw.decode("utf-8", "replace")}

    @staticmethod
    def _q(value) -> str:
        return urllib.parse.quote(str(value), safe="")

    # ---- machines ---------------------------------------------------------------

    def list_machines(self, app: str) -> list:
        out = self._call("GET", f"/apps/{self._q(app)}/machines")
        return out if isinstance(out, list) else []

    def get_machine(self, app: str, machine_id: str) -> dict:
        return self._call("GET", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}")

    def create_machine(self, app: str, config: dict, region: str = "", name: str = "",
                       min_secrets_version: int = None, skip_launch: bool = False) -> dict:
        body = {"config": config}
        if region:
            body["region"] = region
        if name:
            body["name"] = name
        if min_secrets_version is not None:
            body["min_secrets_version"] = int(min_secrets_version)
        if skip_launch:
            body["skip_launch"] = True
        return self._call("POST", f"/apps/{self._q(app)}/machines", body, timeout=120)

    def update_machine(self, app: str, machine_id: str, config: dict, min_secrets_version: int = None,
                       skip_launch: bool = False) -> dict:
        """Replace the machine's config. With containers this reboots the WHOLE
        machine: Fly has no per-container update."""
        body = {"config": config}
        if min_secrets_version is not None:
            body["min_secrets_version"] = int(min_secrets_version)
        if skip_launch:
            body["skip_launch"] = True
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}", body, timeout=120)

    def wait(self, app: str, machine_id: str, state: str = "started", timeout: float = 120,
             instance_id: str = "") -> dict:
        """Block until the machine reaches `state`. The API caps one wait at 60 s,
        so longer waits are a loop; a 408 is "not yet"."""
        deadline = self._clock() + timeout
        while True:
            left = max(1, min(60, int(deadline - self._clock())))
            q = f"state={self._q(state)}&timeout={left}"
            if instance_id:
                q += f"&instance_id={self._q(instance_id)}"
            try:
                return self._call("GET", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/wait?{q}",
                                  timeout=left + 15)
            except MachinesError as e:
                if e.status not in (408, 504) or self._clock() >= deadline:
                    raise

    def stop_machine(self, app: str, machine_id: str) -> dict:
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/stop", {})

    def start_machine(self, app: str, machine_id: str) -> dict:
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/start", {})

    def cordon(self, app: str, machine_id: str) -> dict:
        """Take the machine out of Fly's proxy routing. A stopped machine behind a
        fly-replay edge is otherwise STARTED by the replay even with its service's
        autostart off (real Fly, 2026-10-05: event source "proxy"); cordoned, the
        replay fails and the edge's fallback answers instead."""
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/cordon", {})

    def uncordon(self, app: str, machine_id: str) -> dict:
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/uncordon", {})

    def restart_machine(self, app: str, machine_id: str) -> dict:
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/restart", {})

    def destroy_machine(self, app: str, machine_id: str, force: bool = True) -> dict:
        return self._call("DELETE", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}"
                          + ("?force=true" if force else ""))

    def exec(self, app: str, machine_id: str, command: list, container: str = "", timeout: int = 30) -> dict:
        """Run `command` in one container. Returns {stdout, stderr, exit_code}."""
        body = {"command": list(command), "timeout": int(timeout)}
        if container:
            body["container"] = container
        return self._call("POST", f"/apps/{self._q(app)}/machines/{self._q(machine_id)}/exec", body,
                          timeout=timeout + 30)

    def container_states(self, machine: dict) -> dict:
        """{container name: state} from a GET machine ("healthy", "started",
        "stopped", ...). Per-container state lives only here: `flyctl logs` lines
        carry no container name."""
        return {c.get("name"): c.get("state", "") for c in machine.get("containers") or []}

    def wait_containers(self, app: str, machine_id: str, want: dict, timeout: float = 600,
                        poll: float = 5, progress=None) -> dict:
        """Poll until every container in `want` ({name: "healthy"|"exited"}) is
        there. Returns the last states; raises MachinesError naming the laggards.
        "exited" means it ran and finished (an init container): Fly reports that
        container as stopped."""
        say = progress or (lambda m: None)
        deadline = self._clock() + timeout
        last = {}
        while True:
            states = self.container_states(self.get_machine(app, machine_id))
            if states != last:
                say("containers: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))
                last = states
            pending = {n: w for n, w in want.items()
                       if not (states.get(n) == "healthy" if w == "healthy" else states.get(n) in ("stopped", "exited"))}
            if not pending:
                return states
            if self._clock() >= deadline:
                raise MachinesError("containers not ready after %ds: %s" % (
                    timeout, ", ".join(f"{n} is {states.get(n) or 'missing'} (want {w})" for n, w in sorted(pending.items()))))
            self._sleep(poll)

    # ---- secrets -----------------------------------------------------------------

    def set_secret(self, app: str, name: str, value: str) -> int:
        """Set an app secret; returns the secrets version that includes it (pass
        it as min_secrets_version so the machine sees it). Containers of an
        API-created machine only get a secret they list in their own `secrets`.
        Neither the value nor the response (which echoes it) is ever reported."""
        out = self._call("POST", f"/apps/{self._q(app)}/secrets/{self._q(name)}", {"value": value}, quiet=True)
        version = out.get("version", out.get("Version"))
        if version is None:
            raise MachinesError(f"setting secret {name} on {app} returned no version")
        return int(version)

    def list_secrets(self, app: str) -> list:
        """Names of the app's secrets (values are never returned by this call)."""
        out = self._call("GET", f"/apps/{self._q(app)}/secrets", quiet=True)
        items = out.get("secrets", out) if isinstance(out, dict) else out
        return sorted({s.get("name") or s.get("Name") for s in items or [] if isinstance(s, dict)} - {None})
