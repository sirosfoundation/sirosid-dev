"""FlyClient - the flyctl operations an instance's deployment needs, as an object.

This replaces the module-level run_fly() and friends in scripts/fly_common.py,
which hard-coded one org, shared one global "docker is authed" flag and called
subprocess directly. As an object it can be pointed at ANY org with ANY token
(a hosted service manages instances in its own dedicated org, never the
developer's), and the process runner is a parameter - tests inject a fake
flyctl (tests/fakefly.py) instead of patching subprocess.

It raises FlyError, never SystemExit: that decision belongs to the caller.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .components import FLY_REGION_FALLBACK, VOLUME_SIZE_GB

DEFAULT_ORG = "sirosfoundation"


class FlyError(RuntimeError):
    """A flyctl operation failed in a way the caller cannot carry on past."""


def detect_region() -> str:
    """Fly's own suggestion for where to run: the anycast edge nearest here.

    Every Fly API response carries `fly-request-id: <id>-<region>`, and the
    region is whichever edge anycast routed to - which is what `fly launch`
    means by the nearest region. One HEAD request, short timeout, and any
    failure falls back rather than blocking a deploy.

    Note this is genuinely per-machine: from Stockholm it answers 'fra', not
    'arn', so it is Fly's routing view rather than raw geography. That is the
    right thing to follow - it is the same view that decides where traffic to
    the deployed apps lands.
    """
    req = urllib.request.Request("https://api.fly.io/", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            request_id = resp.headers.get("fly-request-id", "")
    except urllib.error.HTTPError as e:
        # The bare path 404s, but the edge still stamps the header on its way
        # out - that response is just as good as a 200 for this.
        request_id = e.headers.get("fly-request-id", "")
    except Exception:
        return ""
    region = request_id.rsplit("-", 1)[-1] if "-" in request_id else ""
    # Shape check rather than a hardcoded region list, which would go stale:
    # every Fly region code is three lowercase letters.
    return region if len(region) == 3 and region.isalpha() and region.islower() else ""


class FlyClient:
    def __init__(self, org: str = DEFAULT_ORG, token: str = "", runner=None, docker=None,
                 out=None, err=None):
        """org: the Fly org apps are created in. token: FLY_API_TOKEN for every
        call ("" = whatever flyctl is already logged in as). runner/docker:
        callables with subprocess.run's signature; default to subprocess.run,
        looked up at call time. out/err: status and diagnostic sinks.
        """
        self.org = org
        self.token = token
        self._runner = runner or (lambda *a, **k: subprocess.run(*a, **k))
        self._docker_runner = docker or (lambda *a, **k: subprocess.run(*a, **k))
        self._out = out or print
        self._err = err or (lambda msg: print(msg, file=sys.stderr))
        self._docker_authed = False

    def _say(self, msg=""):
        self._out(msg)

    def _warn(self, msg=""):
        self._err(msg)

    def _env(self):
        if not self.token:
            return None
        env = dict(os.environ)
        env["FLY_API_TOKEN"] = self.token
        return env

    def _docker(self, *args, check=False, capture_output=False):
        return self._docker_runner(["docker", *args], check=check, capture_output=capture_output)

    def run(self, *args, check=True, capture=False):
        cmd = ["flyctl"] + list(args)
        self._warn("+ " + " ".join(cmd))
        kw = {"text": True, "capture_output": capture}
        env = self._env()
        if env is not None:
            kw["env"] = env
        result = self._runner(cmd, **kw)
        if check and result.returncode != 0:
            if capture:
                self._warn(result.stdout)
                self._warn(result.stderr)
            raise FlyError(f"flyctl {args[0]} failed (exit {result.returncode})")
        return result

    def app_exists(self, name: str) -> bool:
        result = self.run("apps", "list", "--json", check=False, capture=True)
        if result.returncode != 0:
            return False
        apps = json.loads(result.stdout or "[]")
        return any(a.get("Name") == name for a in apps)

    def ensure_app(self, name: str, network: str = None, allocate_public_ips: bool = False):
        """allocate_public_ips=True is only needed for a raw TCP-passthrough
        component (write_fly_toml's tcp_passthrough_port, e.g. "conformance") -
        confirmed empirically: unlike [http_service], a [[services]] block does
        NOT trigger Fly's usual automatic public IP allocation (`flyctl ips list`
        came back completely empty for such an app after a normal deploy, and its
        hostname didn't resolve anywhere, even though the machine itself was
        healthy). Both allocate-v4/v6 calls are idempotent (a no-op printing the
        existing IP if one's already allocated), so this is safe to call on
        every deploy, not just the first.
        """
        if self.app_exists(name):
            self._say(f"app {name} already exists")
        else:
            args = ["apps", "create", name, "-o", self.org, "--yes"]
            if network:
                args += ["--network", network]
            self.run(*args)
        if allocate_public_ips:
            self.run("ips", "allocate-v4", "--shared", "-a", name)
            self.run("ips", "allocate-v6", "-a", name)

    def is_local_docker_image(self, ref: str) -> bool:
        """True only for a bare image reference with no registry/namespace
        prefix (no '/') that's also present in the local Docker daemon - e.g.
        wallet-backend-e2e-test:local, exactly what `make up REBUILD=yes` /
        docker compose build already produce. The no-'/' check is deliberate:
        a real registry ref always has at least one ('ghcr.io/org/image', or
        even bare 'org/image'), so this never mistakes an intentionally-remote
        --images value for a local build just because it also happens to be
        cached locally (e.g. from a prior `docker pull` done only to inspect it).
        """
        if "/" in ref:
            return False
        try:
            result = self._docker("image", "inspect", ref, capture_output=True)
        except FileNotFoundError:
            return False  # no local `docker` at all - fall through to a normal pull attempt
        return result.returncode == 0

    def push_local_image(self, app: str, local_ref: str) -> str:
        """Tags and pushes a locally-built image (see is_local_docker_image) into
        `app`'s own registry.fly.io namespace and returns the pushed ref, so the
        caller can deploy it with a normal `-i <ref>` exactly like any other
        --images override - no manual `docker tag`/`docker push`/`flyctl auth
        docker` required from the developer. registry.fly.io namespaces images
        per Fly app, so `app` must already exist (self.ensure_app() always runs
        before this is called in deploy_component()) - Fly rejects a push
        against an app name it doesn't recognize. Tagged with the push time
        rather than reused as-is: repushing under a fixed tag would still work
        (Fly resolves the manifest fresh on every deploy, no client-side image
        cache involved), but a unique tag makes it obvious in the Fly dashboard
        which push a given deploy actually came from.
        """
        if not self._docker_authed:
            # One-time per run - reuses the developer's own `flyctl auth login`
            # session, no separate registry credential to manage.
            self.run("auth", "docker")
            self._docker_authed = True
        remote_ref = f"registry.fly.io/{app}:local-{int(time.time())}"
        self._docker("tag", local_ref, remote_ref, check=True)
        self._docker("push", remote_ref, check=True)
        return remote_ref

    def ensure_running(self, app: str):
        """`fly deploy` on a previously-stopped machine (e.g. crash-looped in an
        earlier attempt, or a service-less internal app with no autostart path
        at all) updates its config but doesn't necessarily start it - confirmed
        empirically (vc-issuer stayed 'stopped' after a config-only update
        following an earlier crash). Explicitly starts any machine still not
        running post-deploy, for every component, not just internal-only ones.
        """
        result = self.run("machine", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return
        try:
            machines = json.loads(result.stdout or "[]")
        except ValueError:
            return
        for m in machines:
            # Only a machine that is actually at rest. Right after a deploy a
            # machine is briefly 'created'/'starting'/'replacing', and a start
            # request then fails with a failed_precondition that only looks alarming.
            if m.get("state") in ("stopped", "suspended"):
                self.run("machine", "start", m["id"], "-a", app, check=False)

    def machine_private_ip(self, app: str) -> str | None:
        """The 6PN IPv6 address of an app's (first) machine - queried via the
        Fly API, not DNS (no need to be on the WireGuard mesh to run this from a
        developer's own laptop, unlike an actual `.internal` lookup). Used for
        conformance-suite-nginx's hardcoded `proxy_pass http://server:8080` (no
        env var to retarget it - see CONFORMANCE_COMPONENTS): baking this literal
        IP into that app's own /etc/hosts as "server" lets its unmodified,
        published config resolve correctly without either forking the image or
        relying on a Docker-only `resolver 127.0.0.11` directive that doesn't
        exist on Fly's network.
        """
        result = self.run("machine", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return None
        try:
            machines = json.loads(result.stdout or "[]")
        except ValueError:
            return None
        return machines[0]["private_ip"] if machines else None

    def wait_for_checks(self, app: str, timeout: int = 90, poll_interval: int = 3):
        """Polls `flyctl checks list` until every check on `app` reports healthy,
        or gives up after `timeout` seconds (printing a warning, not failing the
        whole deploy - a slow-to-report check shouldn't block the rest of the
        environment when the machine itself did start).

        Generalizes what used to be a single `time.sleep(10)` after mongodb's
        deploy specifically (a pragmatic guess at how long Fly's 6PN DNS/routing
        takes to propagate for a brand-new machine) into an actual wait for a
        real signal, for every internal-only component that now has a machine
        check (write_fly_toml's `internal_check`) - not just mongodb.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.run("checks", "list", "-a", app, "--json", check=False, capture=True)
            if result.returncode == 0:
                try:
                    # {machine_id: [check, ...], ...} - NOT a flat list.
                    by_machine = json.loads(result.stdout or "{}")
                except ValueError:
                    by_machine = {}
                checks = [c for machine_checks in by_machine.values() for c in machine_checks]
                if checks and all(c.get("status") == "passing" for c in checks):
                    self._say(f"{app}: all checks passing")
                    return
            time.sleep(poll_interval)
        self._say(f"{app}: checks did not report passing within {timeout}s - continuing anyway "
              f"(check `flyctl checks list -a {app}` if the next component fails to reach it)",
              file=sys.stderr)

    def destroy_app(self, name: str) -> bool:
        """True if the app existed and was destroyed, False if there was nothing to do."""
        if not self.app_exists(name):
            self._say(f"app {name} does not exist, skipping")
            return False
        self.run("apps", "destroy", name, "--yes")
        return True

    def list_apps(self) -> list:
        """Names of every app in the org (the sweeper's view of what exists)."""
        result = self.run("apps", "list", "--json", check=False, capture=True)
        if result.returncode != 0:
            raise FlyError(f"could not list apps in org {self.org} (exit {result.returncode})")
        return [a["Name"] for a in json.loads(result.stdout or "[]")]

    def list_machines(self, app: str) -> list:
        result = self.run("machine", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout or "[]")
        except ValueError:
            return []

    def stop_machines(self, app: str):
        """For `fly-down --keep-data`: keep the app and its volume, stop paying
        for a running machine. `fly-up` starts it again (ensure_running)."""
        for m in self.list_machines(app):
            if m.get("state") not in ("stopped", "destroyed"):
                self.run("machine", "stop", m["id"], "-a", app, check=False)

    def list_volumes(self, app: str) -> list:
        result = self.run("volumes", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return []
        try:
            return json.loads(result.stdout or "[]")
        except ValueError:
            return []

    def ensure_volume(self, app: str, name: str, region: str, size_gb: int = VOLUME_SIZE_GB) -> dict:
        """Create `name` in `region` for `app` unless one already exists.

        A volume pins its machine to a region, so an existing volume in a
        DIFFERENT region than this run wants is a hard error rather than a
        silent second volume: Fly would place the machine with the new volume
        and the old data would sit orphaned, which is the one outcome persistent
        storage exists to prevent. Relocating an environment means clearing its
        data (make fly-storage-clear / fly-down without KEEP_DATA) first.
        """
        existing = [v for v in self.list_volumes(app) if v.get("name") == name and v.get("state") != "destroyed"]
        if existing:
            vol = existing[0]
            if region and vol.get("region") and vol["region"] != region:
                raise FlyError(
                    f"{app}: volume {name} ({vol['id']}) lives in region {vol['region']} but this run targets "
                    f"{region}. A volume pins its machine, so the environment cannot move without losing the data.\n"
                    f"  Either redeploy in {vol['region']} (REGION={vol['region']}, or pin region: in "
                    f"environments/<name>.yaml), or clear the data first: make fly-storage-clear ENV=<name> "
                    f"(or make fly-down ENV=<name> without KEEP_DATA) and deploy again.")
            self._say(f"{app}: volume {name} exists ({vol['id']}, {vol.get('region')}, {vol.get('size_gb')} GB)")
            return {**vol, "created": False}
        result = self.run("volumes", "create", name, "-a", app, "-r", region or FLY_REGION_FALLBACK,
                         "-s", str(size_gb), "--yes", "--json", capture=True)
        try:
            vol = json.loads(result.stdout or "{}")
        except ValueError:
            vol = {}
        self._say(f"{app}: created volume {name} ({vol.get('id', '?')}, {region}, {size_gb} GB)")
        # `created` tells the caller the data is EMPTY - the one moment a Mongo
        # root password may (must, for an app that predates volumes and still
        # carries a rotated secret nobody knows) be set fresh.
        return {**vol, "created": True}

    @staticmethod
    def machine_has_mount(machine: dict, volume: str) -> bool:
        """Whether a machine's config mounts the named volume (by volume name or
        id - the API reports the id, the fly.toml names the volume)."""
        for m in (machine.get("config") or {}).get("mounts") or []:
            if m.get("name") == volume or m.get("volume") == volume or str(m.get("volume", "")).startswith("vol_"):
                return True
        return False

    def assert_volume_mounted(self, app: str, volume: str):
        """Fail the deploy if the app's machine did not come up with its volume.
        A mount that silently does not apply means the data is on the machine's
        ephemeral disk again - exactly the state persistent storage exists to end,
        and the next redeploy would erase it."""
        machines = self.list_machines(app)
        if not machines:
            raise FlyError(f"{app}: no machine after deploy - cannot confirm volume {volume} is mounted")
        if not all(self.machine_has_mount(m, volume) for m in machines):
            raise FlyError(
                f"{app}: machine came up WITHOUT the volume mount ({volume}). Its data would be ephemeral "
                f"again and lost on the next deploy. Check the generated {app.split('-')[-1]}.fly.toml has a "
                f"[mounts] block and `flyctl machine list -a {app} --json` shows config.mounts.")
        self._say(f"{app}: volume {volume} mounted")

    def destroy_machines_without_mount(self, app: str):
        """A machine created before this component had a volume cannot have one
        attached after the fact - `fly deploy` refuses to add a mount to an
        existing machine. Its data was ephemeral anyway (that is the state this
        migration ends), so destroy it and let the deploy create a fresh one on
        the volume."""
        for m in self.list_machines(app):
            if not (m.get("config") or {}).get("mounts"):
                self._say(f"{app}: machine {m['id']} predates the volume (no mount) - replacing it")
                self.run("machine", "destroy", m["id"], "-a", app, "--force", check=False)

    def create_deploy_token(self, app: str, name: str = "sirosid-env-admin", expiry: str = "8760h") -> str:
        """An app-scoped deploy token: enough for the Machines API on THIS app
        (list/stop/start), nothing on any other app in the org. Created fresh on
        every fly-up (they are cheap and env-admin's secret is re-set with the
        new set), and revoked by fly-down via self.revoke_tokens()."""
        result = self.run("tokens", "create", "deploy", "-a", app, "--name", name, "--expiry", expiry, "--json",
                         capture=True)
        try:
            data = json.loads(result.stdout or "{}")
            token = data.get("token") or ""
        except ValueError:
            token = ""
        if not token:
            # Older flyctl prints the bare token (sometimes prefixed "FlyV1 ").
            token = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
        if not token:
            raise FlyError(f"could not create a deploy token for {app}: {result.stderr}")
        return token

    def revoke_tokens(self, app: str, name: str = "sirosid-env-admin"):
        """Best effort: an app-scoped token is useless once the app is destroyed,
        so this is hygiene, not security-critical."""
        result = self.run("tokens", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return
        try:
            tokens = json.loads(result.stdout or "[]")
        except ValueError:
            return
        for t in tokens:
            if t.get("Name", t.get("name")) == name:
                self.run("tokens", "revoke", t.get("ID", t.get("id")), check=False)

    def read_machine_file(self, app: str, path: str) -> str:
        """Read a file from the app's running machine over `fly ssh console`.

        Fly secrets cannot be read back through the API, but a `--file-secret`
        IS readable from inside the machine it is mounted into - which is how a
        developer who did not do the last deploy (and so has no local secret
        cache) can recover mongodb's root password instead of deploying a
        mismatched one. Empty string on any failure."""
        result = self.run("ssh", "console", "-a", app, "-C", f"cat {path}", check=False, capture=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    def existing_secret_names(self, app: str) -> set:
        result = self.run("secrets", "list", "-a", app, "--json", check=False, capture=True)
        if result.returncode != 0:
            return set()
        try:
            return {s["name"] for s in json.loads(result.stdout or "[]")}
        except (ValueError, KeyError):
            return set()

    def ensure_secret(self, app: str, key: str, value: str, force: bool = False):
        """Idempotent by default: never rotates a secret that's already set,
        mirroring render-helm-config.py's gen_secret() file-based idempotency.
        Staged (not applied immediately) - the `flyctl deploy` call that follows
        in fly-up.py picks up staged secrets on its own, so there's no running
        machine yet to redundantly restart here on a first-ever deploy.

        force=True skips the existing-value check entirely - only correct for a
        secret whose consumers ALL get redeployed together in the same run with
        the same freshly-generated value (e.g. mongodb's root password: mongo has
        no persistent volume, so every deploy starts from empty data anyway,
        meaning there's no old state a stale password would need to keep
        matching - unlike, say, the VC signing key, where an old private key
        deployed to only some consumers alongside new public certs on others
        would break credential verification).

        Every current caller mounts this secret into a file via `--file-secret`
        (not consumed as a plain env var) - and `--file-secret` base64-DECODES
        the stored secret value when writing the destination file (confirmed
        empirically: a plaintext value came out the other end as binary garbage;
        storing the base64 encoding of that same value produced the correct
        plaintext file). So the value handed to `flyctl secrets set` here must
        already be base64-encoded, or every `--file-secret`-mounted consumer
        silently gets a corrupted file - previously unnoticed for jwtSecret/
        adminToken only because they're opaque random tokens nothing else needs
        to match; it broke mongodb's root password and the VC signing key
        outright (auth failures / would-be signing failures) since those must
        equal a SPECIFIC value another component also holds.
        """
        if not force and key in self.existing_secret_names(app):
            self._say(f"secret {key} already set on {app}, leaving as-is")
            return
        encoded = base64.b64encode(value.encode()).decode()
        self.run("secrets", "set", f"{key}={encoded}", "-a", app, "--stage")
