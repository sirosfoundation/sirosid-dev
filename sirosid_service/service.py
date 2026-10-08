"""ControlPlane - every rule about who may do what to which instance.

Front ends (HTTP, MCP, a CLI) authenticate a caller and obtain a Principal; they
call these methods and nothing else. All ownership, quota, capability and policy
checks live here so no front end can forget one.

Fly is reached only through sirosid_core's FlyClient, in the service's own sandbox
org with its own credential. Slow work (deploy, reset) runs through an injected
Runner: threads in production, inline in tests.
"""
import json
import logging
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sirosid_core import policy as policy_mod
from sirosid_core import state as state_mod
from sirosid_core.components import component_names
from sirosid_core.deploy import DeployError, deploy_instance, register_vc_services
from sirosid_core.fly import FlyClient, FlyError
from sirosid_core.lifecycle import destroy_instance, reset_single_machine, start_instance, stop_instance
from sirosid_core.machines import MachinesClient, MachinesError
from sirosid_core.naming import LAYOUT_SINGLE_MACHINE, Naming
from sirosid_core.singlemachine import deploy_instance_single_machine
from sirosid_core.policy import PlatformPolicy, PolicyError
from sirosid_core.resources import Resources
from sirosid_core.spec import InstanceSpec

from .db import Database, SealedStateStore, delete_state, hash_token
from .vault import KEY_CHECK_PLAINTEXT, Locked, SessionKeys, VaultError, aad

log = logging.getLogger("sirosid.service")

DAY = 86400.0
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
ID_LENGTH = 8
# Ids that would read as an impersonation or an internal name. A generated id is
# random, so this only matters if the generator is ever swapped for a chosen one.
RESERVED_IDS = frozenset({"admin", "console", "status", "www", "api", "mail", "login", "siros", "sirosid", "support"})

# Instance statuses. Every one but 'destroyed' counts against quotas and is on the
# reaper's clock. creating/resetting/reconfiguring end in running or failed when their
# background job finishes; stopping/starting are reserved (stop and start are synchronous).
LIVE = ("creating", "running", "stopped", "stopping", "starting", "resetting", "reconfiguring", "failed")
STATUSES = LIVE + ("destroyed",)
# Where a reconfigure is accepted. NOT 'stopped': a redeploy starts every machine (the
# apps layout's ensure_running, the single-machine update), so applying a config to a
# stopped instance would silently start it and bill it; the caller starts it first.
RECONFIGURABLE = ("running", "failed")

# The audit actions that describe an instance, as instance_activity shows them, and the
# detail keys it may pass on. Both are allow-lists: an audit row's target is a bare
# string (a config NAME is one too), and a new detail key must be looked at before an
# owner - or the model reading their activity - sees it.
INSTANCE_ACTIONS = frozenset({
    "create_instance", "deployed", "deploy_failed", "stop_instance", "start_instance", "reset_instance",
    "reset_done", "reset_failed", "destroy_instance", "destroy_failed", "set_keep", "keep_lapsed",
    "reconfigure_instance", "reconfigured", "reconfigure_failed"})
ACTIVITY_DETAIL_KEYS = frozenset({"kept", "keep", "images", "error", "failed", "config_name", "changed"})
SYSTEM_ACTORS = frozenset({"system", "reaper", "sweeper"})


class ServiceError(Exception):
    """Base for errors a front end should show to the caller."""


class NotFound(ServiceError):
    pass


class Forbidden(ServiceError):
    pass


class NotSignedIn(Forbidden):
    """No valid web session: the caller must authenticate (HTTP 401, not 403)."""


class QuotaExceeded(ServiceError):
    pass


class InvalidState(ServiceError):
    pass


class InvalidInvite(ServiceError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str = "member"
    capabilities: frozenset = frozenset()
    # Set once the user has unlocked their data with their passkey (see vault.py).
    # Without it a principal can still do everything that needs only plaintext
    # metadata; anything that touches the user's own data raises Locked.
    session_id: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Limits:
    ttl_days: float = 3.0
    global_max_instances: int = 10
    default_max_concurrent: int = 2
    sweep_grace_seconds: float = 3600.0
    lapse_grace_days: float = 1.0
    invite_max_days: float = 30.0


class SyncRunner:
    """Runs jobs inline - for tests and one-shot CLIs."""

    def submit(self, fn, *args):
        fn(*args)


class ThreadRunner:
    def __init__(self, workers=4):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cp-job")

    def submit(self, fn, *args):
        self._pool.submit(self._run, fn, *args)

    @staticmethod
    def _run(fn, *args):
        # A pool swallows a job's exception into a Future nobody reads: log it.
        try:
            fn(*args)
        except Exception:                                   # noqa: BLE001
            log.exception("background job %s failed", getattr(fn, "__name__", fn))


def generate_instance_id(rng=secrets) -> str:
    return "".join(rng.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def _json(v):
    return json.dumps(v, sort_keys=True)


class ControlPlane:
    def __init__(self, db: Database, fly: FlyClient, resources: Resources, platform: PlatformPolicy = None,
                 limits: Limits = None, runner=None, register: Callable = None, clock=None,
                 id_generator: Callable[[], str] = None, machines=None, registry=None):
        self.db = db
        self.fly = fly
        self.resources = resources
        self.platform = platform or PlatformPolicy(env_admin=False)
        self.limits = limits or Limits()
        self.runner = runner or SyncRunner()
        self.register = register
        self.clock = clock or db.clock
        self._new_id = id_generator or generate_instance_id
        self.keys = SessionKeys(self.clock)
        # The Machines API client for single-machine instances: the same org
        # credential as `fly`. Built lazily so an apps-layout service needs none.
        self._machines = machines
        # Where single-machine config bundles are pushed (sirosid_core.oci.Registry);
        # None = registry.fly.io with a credential minted per push.
        self.registry = registry

    @property
    def machines(self) -> MachinesClient:
        if self._machines is None:
            self._machines = MachinesClient(self.fly.token)
        return self._machines

    # ---- principals -------------------------------------------------------

    def principal_for(self, user_id: str, session_id: str = "") -> Principal:
        u = self._user(user_id)
        if u["disabled"]:
            raise Forbidden("this account is disabled")
        return Principal(u["id"], u["role"], frozenset(json.loads(u["capabilities"])), session_id)

    # ---- unlocking (the user's passkey produces the key; see vault.py) ---------

    def begin_session(self, user_id: str, main_key: bytes, ttl: float = None) -> Principal:
        """The browser has unlocked the user's container with their passkey and hands
        over the main key for this session. Held in memory only, until it expires.

        The first unlock stores a key-check blob; every later one must open it, so a
        wrong key (a different container, a bug) is refused instead of being used to
        seal data that the right key could then never read."""
        u = self._user(user_id)
        if u["disabled"]:
            raise Forbidden("this account is disabled")
        from .vault import Sealer
        sealer = Sealer(main_key)
        check_aad = aad(user_id, "keycheck")
        if u["key_check"] is None:
            self.db.execute("UPDATE users SET key_check=? WHERE id=?", (sealer.seal(KEY_CHECK_PLAINTEXT, check_aad), user_id))
        else:
            try:
                ok = sealer.open(bytes(u["key_check"]), check_aad) == KEY_CHECK_PLAINTEXT
            except VaultError:
                ok = False
            if not ok:
                self.db.audit(user_id, "unlock_refused", user_id)
                raise Forbidden("that key does not open this account's data")
        sid = self.keys.put(user_id, main_key, ttl)
        self.db.audit(user_id, "unlock", user_id)
        return self.principal_for(user_id, sid)

    def end_session(self, who: Principal):
        self.keys.drop(who.session_id)

    def _sealer(self, who: Principal):
        return self.keys.get(who.session_id, who.user_id)

    def share_session(self, who: Principal, ttl: float) -> str:
        """A separate key session for an application acting for this user (see SessionKeys.clone).
        Needs the user to be unlocked right now."""
        return self.keys.clone(who.session_id, who.user_id, ttl)

    def drop_session(self, session_id: str):
        self.keys.drop(session_id)

    def session_alive(self, session_id: str, user_id: str) -> bool:
        try:
            self.keys.get(session_id, user_id)
            return True
        except Locked:
            return False

    def session_expiry(self, session_id: str, user_id: str) -> float:
        return self.keys.expires_at(session_id, user_id)

    def is_unlocked(self, who: Principal) -> bool:
        """True only while the session's key is still held (it expires; the session id outlives it)."""
        try:
            self._sealer(who)
            return True
        except Locked:
            return False

    def set_privatedata(self, who: Principal, container: bytes):
        """The browser's wrapped main-key container (one entry per passkey), stored
        OPAQUE: the server never parses it and never sees a PRF output."""
        if not isinstance(container, (bytes, bytearray)) or not 0 < len(container) <= 64 * 1024:
            raise ServiceError("the container must be 1 byte to 64 KB")
        self.db.execute("UPDATE users SET privatedata=? WHERE id=?", (bytes(container), who.user_id))
        self.db.audit(who.user_id, "set_privatedata", who.user_id, bytes=len(container))

    def get_privatedata(self, who: Principal) -> Optional[bytes]:
        r = self._user(who.user_id)["privatedata"]
        return bytes(r) if r is not None else None

    def _user(self, user_id):
        u = self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not u:
            raise NotFound("no such user")
        return u

    def bootstrap_admin(self, name: str, email: str = "") -> Principal:
        """Create the first admin (from the command line, on the host). Refuses once any admin exists."""
        with self.db.transaction() as d:
            if d.one("SELECT 1 AS x FROM users WHERE role='admin'"):
                raise Forbidden("an admin already exists; admins create further invites")
            uid = "u_" + secrets.token_hex(6)
            d.execute("INSERT INTO users(id,name,email,role,capabilities,max_concurrent,max_kept,created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, name, email, "admin", _json(list(policy_mod.CAPABILITIES)), self.limits.default_max_concurrent, 5,
                       self.clock()))
        self.db.audit(uid, "bootstrap_admin", uid)
        return self.principal_for(uid)

    def bootstrap_invite(self, days_valid: float = 1.0) -> str:
        """An invite for the FIRST admin, issued from the command line on the host.

        Unlike bootstrap_admin it creates no account: whoever holds the token enrols a
        passkey in the browser and becomes the admin, exactly like any later user. It
        refuses once any admin exists, so it cannot be used to mint more of them.
        """
        if self.db.one("SELECT 1 AS x FROM users WHERE role='admin'"):
            raise Forbidden("an admin already exists; admins create further invites")
        token = secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT INTO invites(token_hash,created_by,role,capabilities,max_concurrent,max_kept,email,expires_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (hash_token(token), "system", "admin", _json(list(policy_mod.CAPABILITIES)), self.limits.default_max_concurrent, 5, "",
             self.clock() + days_valid * DAY, self.clock()))
        self.db.audit("system", "bootstrap_invite", hash_token(token)[:12])
        return token

    # ---- invites (admin) ---------------------------------------------------

    def create_invite(self, who: Principal, *, capabilities=(), role="member", email="", days_valid=7.0,
                      max_concurrent: int = None, max_kept: int = 0, kept_for_days: float = None) -> str:
        """Returns the token ONCE; only its hash is stored."""
        self._require_admin(who)
        caps = sorted(set(capabilities))
        bad = [c for c in caps if c not in policy_mod.CAPABILITIES]
        if bad:
            raise ServiceError(f"unknown capability: {', '.join(bad)}")
        if role not in ("member", "admin"):
            raise ServiceError("role must be 'member' or 'admin'")
        if not 0 < days_valid <= self.limits.invite_max_days:
            raise ServiceError(f"an invite is valid for 1 to {self.limits.invite_max_days:g} days")
        token = secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT INTO invites(token_hash,created_by,role,capabilities,max_concurrent,max_kept,kept_for_days,email,expires_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (hash_token(token), who.user_id, role, _json(caps),
             max_concurrent if max_concurrent is not None else self.limits.default_max_concurrent, max_kept, kept_for_days,
             email.strip().lower(), self.clock() + days_valid * DAY, self.clock()))
        self.db.audit(who.user_id, "create_invite", hash_token(token)[:12], role=role, capabilities=caps, email=email)
        return token

    def revoke_invite(self, who: Principal, token_or_prefix: str):
        self._require_admin(who)
        rows = self.db.all("SELECT token_hash FROM invites WHERE token_hash=? OR token_hash LIKE ?",
                           (hash_token(token_or_prefix), token_or_prefix + "%"))
        if len(rows) != 1:
            raise NotFound("no unique invite matches")
        self.db.execute("UPDATE invites SET revoked=1 WHERE token_hash=?", (rows[0]["token_hash"],))
        self.db.audit(who.user_id, "revoke_invite", rows[0]["token_hash"][:12])

    def list_invites(self, who: Principal) -> list:
        self._require_admin(who)
        return [{**r, "token_hash": r["token_hash"][:12]} for r in
                self.db.all("SELECT * FROM invites ORDER BY created_at DESC")]

    def check_invite(self, token: str, email: str = ""):
        """Raises InvalidInvite unless `token` could be redeemed right now. Consumes
        nothing: a passkey ceremony is refused up front, and the invite is spent only
        when the ceremony succeeds."""
        inv = self.db.one("SELECT * FROM invites WHERE token_hash=?", (hash_token(token),))
        if not inv or inv["revoked"] or inv["used_by"] or inv["expires_at"] < self.clock():
            raise InvalidInvite("this invite is not valid (unknown, revoked, already used or expired)")
        if inv["email"] and inv["email"] != email.strip().lower():
            raise InvalidInvite("this invite is bound to a different email address")

    def redeem_invite(self, token: str, name: str, email: str = "") -> Principal:
        """Single use. An invite bound to an email only redeems for that email."""
        with self.db.transaction() as d:
            inv = d.one("SELECT * FROM invites WHERE token_hash=?", (hash_token(token),))
            if not inv or inv["revoked"] or inv["used_by"] or inv["expires_at"] < self.clock():
                raise InvalidInvite("this invite is not valid (unknown, revoked, already used or expired)")
            if inv["email"] and inv["email"] != email.strip().lower():
                raise InvalidInvite("this invite is bound to a different email address")
            uid = "u_" + secrets.token_hex(6)
            kept_until = (self.clock() + inv["kept_for_days"] * DAY) if inv["kept_for_days"] else None
            d.execute("INSERT INTO users(id,name,email,role,capabilities,max_concurrent,max_kept,kept_until,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (uid, name.strip()[:80] or "user", email.strip().lower(), inv["role"], inv["capabilities"],
                       inv["max_concurrent"], inv["max_kept"], kept_until, self.clock()))
            d.execute("UPDATE invites SET used_by=?, used_at=? WHERE token_hash=?", (uid, self.clock(), inv["token_hash"]))
        self.db.audit(uid, "redeem_invite", inv["token_hash"][:12])
        return self.principal_for(uid)

    def disable_user(self, who: Principal, user_id: str):
        """Blocks login and puts every one of the user's instances on the normal expiry clock."""
        self._require_admin(who)
        self._user(user_id)
        self.db.execute("UPDATE users SET disabled=1 WHERE id=?", (user_id,))
        self.keys.drop_user(user_id)
        soon = self.clock() + self.limits.lapse_grace_days * DAY
        self.db.execute("UPDATE instances SET kept=0, expires_at=MIN(COALESCE(expires_at, ?), ?) WHERE owner=? AND status IN (%s)"
                        % ",".join("?" * len(LIVE)), (soon, soon, user_id, *LIVE))
        self.db.audit(who.user_id, "disable_user", user_id)

    def grant(self, who: Principal, user_id: str, *, capabilities=None, max_concurrent=None, max_kept=None,
              kept_for_days=None):
        self._require_admin(who)
        self._user(user_id)
        sets, vals = [], []
        if capabilities is not None:
            bad = [c for c in capabilities if c not in policy_mod.CAPABILITIES]
            if bad:
                raise ServiceError(f"unknown capability: {', '.join(bad)}")
            sets.append("capabilities=?"); vals.append(_json(sorted(set(capabilities))))
        if max_concurrent is not None:
            sets.append("max_concurrent=?"); vals.append(int(max_concurrent))
        if max_kept is not None:
            sets.append("max_kept=?"); vals.append(int(max_kept))
        if kept_for_days is not None:
            sets.append("kept_until=?"); vals.append(self.clock() + kept_for_days * DAY)
        if sets:
            self.db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, user_id))
        self.db.audit(who.user_id, "grant", user_id, capabilities=capabilities, max_concurrent=max_concurrent,
                      max_kept=max_kept, kept_for_days=kept_for_days)

    def _require_admin(self, who: Principal):
        if not who.is_admin:
            raise Forbidden("admins only")

    # ---- saved configs ------------------------------------------------------

    def schema(self) -> dict:
        return policy_mod.schema()

    def templates(self, who: Principal) -> list:
        """Starting-point configs this user can save (those needing a capability they lack are left out)."""
        from sirosid_core.templates import available_templates
        return available_templates(who.capabilities, self.resources)

    def validate_config(self, who: Principal, doc: dict) -> list:
        return [str(p) for p in policy_mod.validate(doc, who.capabilities, self.platform)]

    def save_config(self, who: Principal, name: str, doc: dict) -> dict:
        if not name or len(name) > 64:
            raise ServiceError("a config needs a name of at most 64 characters")
        sealer = self._sealer(who)
        problems = policy_mod.validate(doc, who.capabilities, self.platform)
        if problems:
            raise PolicyError(problems)
        now = self.clock()
        sealed = sealer.seal(_json(doc).encode(), aad(who.user_id, "config", name))
        existing = self.db.one("SELECT id FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        if existing:
            self.db.execute("UPDATE configs SET doc=?, updated_at=? WHERE id=?", (sealed, now, existing["id"]))
            cid = existing["id"]
        else:
            if self.db.one("SELECT COUNT(*) AS n FROM configs WHERE owner=?", (who.user_id,))["n"] >= 50:
                raise QuotaExceeded("at most 50 saved configs")
            cid = "c_" + secrets.token_hex(6)
            self.db.execute("INSERT INTO configs(id,owner,name,doc,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (cid, who.user_id, name, sealed, now, now))
        self.db.audit(who.user_id, "save_config", cid, name=name)
        return {"id": cid, "name": name}

    def list_configs(self, who: Principal) -> list:
        """Names only: listing needs no key, reading a config does."""
        return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"]} for r in
                self.db.all("SELECT * FROM configs WHERE owner=? ORDER BY name", (who.user_id,))]

    def get_config(self, who: Principal, name: str) -> dict:
        r = self.db.one("SELECT * FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        if not r:
            raise NotFound("no such saved config")
        return json.loads(self._sealer(who).open(bytes(r["doc"]), aad(who.user_id, "config", name)))

    def delete_config(self, who: Principal, name: str):
        self.db.execute("DELETE FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        self.db.audit(who.user_id, "delete_config", name)

    # ---- instances ------------------------------------------------------------

    def create_instance(self, who: Principal, *, config: dict = None, config_name: str = None, name: str = "",
                        keep: bool = False) -> dict:
        sealer = self._sealer(who)          # creating touches the user's data: needs an unlocked session
        if config is None:
            config = self.get_config(who, config_name) if config_name else {}
        else:
            config_name = None              # an inline config is not the saved one, whatever its name
        config_name = config_name or None
        # Capabilities are checked NOW, not when the config was saved: a grant may
        # have been withdrawn since.
        user = self._user(who.user_id)
        if user["disabled"]:
            raise Forbidden("this account is disabled")
        caps = frozenset(json.loads(user["capabilities"]))
        with self.db.transaction() as d:
            live = d.one(f"SELECT COUNT(*) AS n FROM instances WHERE owner=? AND status IN ({','.join('?' * len(LIVE))})",
                         (who.user_id, *LIVE))["n"]
            if live >= user["max_concurrent"]:
                raise QuotaExceeded(f"you already have {live} instance(s); your limit is {user['max_concurrent']}")
            total = d.one(f"SELECT COUNT(*) AS n FROM instances WHERE status IN ({','.join('?' * len(LIVE))})", LIVE)["n"]
            if total >= self.limits.global_max_instances:
                raise QuotaExceeded("the service is at capacity; try again later")
            expires = self.clock() + self.limits.ttl_days * DAY
            if keep:
                self._check_keep(d, user)
                expires = None
            iid = self._unused_id(d)
            spec = policy_mod.build_spec(config, iid, caps, self.platform)
            # Plaintext, layout included: stop/start/destroy/the reaper and the
            # sweeper must know which layout an instance is with nobody logged in.
            naming = spec.naming().to_dict()
            # config_name is plaintext metadata (like the instance's own label): the
            # NAME of the saved config it came from, never its content.
            d.execute("INSERT INTO instances(id,owner,name,status,naming,spec,config,created_at,updated_at,expires_at,kept,config_name)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (iid, who.user_id, name[:64], "creating", _json(naming),
                       sealer.seal(_json(spec.to_dict()).encode(), aad(who.user_id, "spec", iid)),
                       sealer.seal(_json(config).encode(), aad(who.user_id, "config-snapshot", iid)),
                       self.clock(), self.clock(), expires, 1 if keep else 0, config_name))
        self.db.audit(who.user_id, "create_instance", iid, kept=keep, images=sorted(spec.images), config_name=config_name)
        self.runner.submit(self._deploy_job, iid, sealer)
        return self.get_instance(who, iid)

    def _check_keep(self, d, user):
        if user["max_kept"] <= 0:
            raise QuotaExceeded("your account has no allowance to keep instances")
        if user["kept_until"] is not None and user["kept_until"] < self.clock():
            raise QuotaExceeded("your keep allowance has expired")
        kept = d.one(f"SELECT COUNT(*) AS n FROM instances WHERE owner=? AND kept=1 AND status IN ({','.join('?' * len(LIVE))})",
                     (user["id"], *LIVE))["n"]
        if kept >= user["max_kept"]:
            raise QuotaExceeded(f"you already keep {kept} instance(s); your allowance is {user['max_kept']}")

    def _unused_id(self, d) -> str:
        for _ in range(50):
            iid = self._new_id()
            if iid in RESERVED_IDS or d.one("SELECT 1 AS x FROM instances WHERE id=?", (iid,)):
                continue
            return iid
        raise ServiceError("could not allocate an instance id")

    def _instance_row(self, who: Principal, iid: str, allow_admin=False) -> dict:
        r = self.db.one("SELECT * FROM instances WHERE id=?", (iid,))
        # Not-yours and nonexistent look the same: ids must not be probeable.
        if not r or (r["owner"] != who.user_id and not (allow_admin and who.is_admin)):
            raise NotFound("no such instance")
        return r

    @staticmethod
    def _public(r: dict) -> dict:
        layout = json.loads(r["naming"]).get("layout", "apps")
        return {"id": r["id"], "name": r["name"], "status": r["status"], "created_at": r["created_at"],
                "expires_at": r["expires_at"], "kept": bool(r["kept"]), "urls": json.loads(r["urls"]), "error": r["error"],
                "config_name": r.get("config_name"), "layout": layout, "reconfigurable": r["status"] in RECONFIGURABLE}

    def list_instances(self, who: Principal, all_users=False) -> list:
        if all_users:
            self._require_admin(who)
            rows = self.db.all("SELECT * FROM instances WHERE status!='destroyed' ORDER BY created_at DESC")
        else:
            rows = self.db.all("SELECT * FROM instances WHERE owner=? AND status!='destroyed' ORDER BY created_at DESC",
                               (who.user_id,))
        return [self._public(r) for r in rows]

    def get_instance(self, who: Principal, iid: str) -> dict:
        return self._public(self._instance_row(who, iid, allow_admin=True))

    def instance_credentials(self, who: Principal, iid: str) -> dict:
        """The owner's way in: public URLs and the wallet-backend admin token. Never an admin's."""
        r = self._instance_row(who, iid)
        if r["status"] not in ("running", "stopped"):
            raise InvalidState(f"the instance is {r['status']}")
        blobs = SealedStateStore(self.db, self._sealer(who), r["owner"]).load(iid)
        token = blobs.get("adminToken", b"").decode().strip()
        return {"urls": json.loads(r["urls"]), "admin_token": token}

    def _set(self, iid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE instances SET {cols}, updated_at=? WHERE id=?", (*fields.values(), self.clock(), iid))

    def _naming(self, r) -> Naming:
        """From the plaintext column: stop, start, destroy, the reaper and the
        sweeper must work with nobody logged in, so they never need the sealed spec."""
        return Naming.from_dict(json.loads(r["naming"]))

    def _machines_for(self, r):
        return self.machines if self._naming(r).single_machine else None

    def _spec(self, r, sealer) -> InstanceSpec:
        return InstanceSpec.from_dict(json.loads(sealer.open(bytes(r["spec"]), aad(r["owner"], "spec", r["id"]))))

    def _deploy_job(self, iid: str, sealer):
        """`sealer` is captured from the requesting session, so a job queued on a
        thread finishes even if the session expires meanwhile - and never outlives
        the request's own authority by being re-derived later."""
        r = self.db.one("SELECT * FROM instances WHERE id=?", (iid,))
        if not r or r["status"] not in ("creating", "resetting"):
            return
        try:
            result = self._deploy(r, self._spec(r, sealer), sealer)
            self._set(iid, status="running", urls=_json(result.urls), error="")
            self.db.audit("system", "deployed", iid)
        except Exception as e:                              # noqa: BLE001 - see _failure
            msg = self._failure(iid, e, "deploy")
            self._set(iid, status="failed", error=msg)
            self.db.audit("system", "deploy_failed", iid, error=msg[:200])

    def _deploy(self, r, spec: InstanceSpec, sealer):
        """Deploy `spec` as instance `r`, with its sealed state (secrets, PKI, Mongo
        password) in a scratch directory that is saved back afterwards. Idempotent:
        a redeploy of a running instance keeps its data and generated state."""
        iid = r["id"]
        store = SealedStateStore(self.db, sealer, r["owner"])
        with state_mod.workdir(store, iid, subdir=f"fly-{iid}") as scratch:
            # Per instance and sealed with its state - never the shared, read-only resources.
            secrets_dir = scratch / f"fly-{iid}" / state_mod.VC_SECRETS_DIR
            if spec.layout == LAYOUT_SINGLE_MACHINE:
                return deploy_instance_single_machine(
                    spec, self.fly, spec.naming(), self.resources, machines=self.machines,
                    rendered_root=scratch, register=self.register, progress=None, registry=self.registry,
                    secrets_dir=secrets_dir)
            return deploy_instance(spec, self.fly, spec.naming(), self.resources, rendered_root=scratch,
                                   register=self.register, progress=None, secrets_dir=secrets_dir)

    @staticmethod
    def _failure(iid: str, e: Exception, what: str) -> str:
        """The error text an owner sees. Anything that is not a known deploy failure (a
        bug, a read-only file, a missing binary) must still end the job in a state the
        owner can see and act on - not 'creating' forever - and is logged in full."""
        if isinstance(e, (DeployError, FlyError, MachinesError, policy_mod.PolicyError, VaultError)):
            return str(e)[:500]
        log.exception("%s of %s failed unexpectedly", what, iid, exc_info=e)
        return f"internal error during {what} ({type(e).__name__}: {e})"[:500]

    # ---- reconfiguring: one environment, its config part of it -------------------

    def get_instance_config(self, who: Principal, iid: str) -> dict:
        """The saved config this instance runs (its own sealed snapshot, not the named
        saved config, which may have changed since). Needs an unlocked session."""
        sealer = self._sealer(who)
        r = self._instance_row(who, iid)
        return json.loads(sealer.open(bytes(r["config"]), aad(r["owner"], "config-snapshot", iid)))

    def reconfigure_instance(self, who: Principal, iid: str, config: dict = None, config_name: str = None) -> dict:
        """Apply a new saved config to an existing instance: validate it against the
        caller's CURRENT capabilities (every problem at once), then redeploy in the
        background keeping its data and generated state - a normal idempotent redeploy
        (apps layout: every component again, changed ones restart; single-machine: the
        machine config is updated, which restarts the machine).

        The working config is never lost: the new spec and config are sealed into
        `pending_*` and only replace the instance's own when the redeploy succeeded. A
        failed redeploy leaves the instance 'failed' with a readable error and its
        previous config still saved, so reconfiguring with that config (or a fixed one)
        is the way back. Refused for a stopped instance (see RECONFIGURABLE)."""
        sealer = self._sealer(who)
        r = self._instance_row(who, iid)
        if config is None and not config_name:
            raise ServiceError("give a config or the name of a saved config")
        if config is None:
            config = self.get_config(who, config_name)
        else:
            config_name = None
        if r["status"] == "stopped":
            raise InvalidState("the instance is stopped: start it first, then reconfigure it")
        if r["status"] not in RECONFIGURABLE:
            raise InvalidState(f"cannot reconfigure an instance that is {r['status']}")
        user = self._user(who.user_id)
        if user["disabled"]:
            raise Forbidden("this account is disabled")
        caps = frozenset(json.loads(user["capabilities"]))
        deployed = self._spec(r, sealer)
        spec = policy_mod.rebuild_spec(config, deployed, caps, self.platform)      # PolicyError: every problem
        old_config = json.loads(sealer.open(bytes(r["config"]), aad(r["owner"], "config-snapshot", iid)))
        changed = sorted(k for k in set(old_config) | set(config) if old_config.get(k) != config.get(k))
        with self.db.transaction() as d:
            # Check-and-set: two reconfigures (or a stop) racing must not both proceed.
            cur = d.one("SELECT status FROM instances WHERE id=?", (iid,))
            if not cur or cur["status"] not in RECONFIGURABLE:
                raise InvalidState(f"cannot reconfigure an instance that is {cur['status'] if cur else 'gone'}")
            d.execute("UPDATE instances SET status='reconfiguring', error='', pending_spec=?, pending_config=?, updated_at=? WHERE id=?",
                      (sealer.seal(_json(spec.to_dict()).encode(), aad(r["owner"], "spec-pending", iid)),
                       sealer.seal(_json(config).encode(), aad(r["owner"], "config-pending", iid)), self.clock(), iid))
        self.db.audit(who.user_id, "reconfigure_instance", iid, config_name=config_name, changed=changed)
        self.runner.submit(self._reconfigure_job, iid, sealer, config_name)
        return self.get_instance(who, iid)

    def _reconfigure_job(self, iid: str, sealer, config_name):
        r = self.db.one("SELECT * FROM instances WHERE id=?", (iid,))
        if not r or r["status"] != "reconfiguring" or r["pending_spec"] is None:
            return
        owner = r["owner"]
        try:
            spec = InstanceSpec.from_dict(json.loads(sealer.open(bytes(r["pending_spec"]), aad(owner, "spec-pending", iid))))
            config = json.loads(sealer.open(bytes(r["pending_config"]), aad(owner, "config-pending", iid)))
            result = self._deploy(r, spec, sealer)
        except Exception as e:                              # noqa: BLE001 - see _failure
            msg = self._failure(iid, e, "reconfigure")
            msg = (f"reconfigure failed: {msg}. The previous config is still this instance's config; reconfigure "
                   f"again with it (get_instance_config) or with a fixed one.")[:600]
            self.db.execute("UPDATE instances SET status='failed', error=?, pending_spec=NULL, pending_config=NULL, updated_at=? "
                            "WHERE id=? AND status='reconfiguring'", (msg, self.clock(), iid))
            self.db.audit("system", "reconfigure_failed", iid, error=msg[:200])
            return
        # Only now does the new config become the instance's. Guarded on the status: a
        # reaper or destroy that ran meanwhile wins.
        self.db.execute(
            "UPDATE instances SET status='running', error='', urls=?, spec=?, config=?, config_name=?, pending_spec=NULL, "
            "pending_config=NULL, updated_at=? WHERE id=? AND status='reconfiguring'",
            (_json(result.urls), sealer.seal(_json(spec.to_dict()).encode(), aad(owner, "spec", iid)),
             sealer.seal(_json(config).encode(), aad(owner, "config-snapshot", iid)), config_name, self.clock(), iid))
        self.db.audit("system", "reconfigured", iid, config_name=config_name)

    # ---- looking at an instance: status, activity ------------------------------------

    def instance_health(self, who: Principal, iid: str) -> dict:
        """Live component status from Fly. Metadata only (no unlock, no secrets),
        bounded in time, and never raises on a Fly problem: `error` says what went wrong."""
        from sirosid_core.health import instance_health
        r = self._instance_row(who, iid)
        out = {"instance": self._public(r), "components": [], "checked_at": self.clock()}
        if r["status"] == "destroyed":
            out["error"] = "the instance is destroyed"
            return out
        try:
            report = instance_health(self.machines, self._naming(r))
        except Exception as e:                              # noqa: BLE001 - e.g. no Machines credential
            log.warning("health of %s: %s", iid, type(e).__name__)
            out["error"] = "could not reach Fly"
            return out
        out["components"] = report.components
        if report.error:
            out["error"] = report.error
        return out

    def instance_activity(self, who: Principal, iid: str, limit: int = 50) -> list:
        """What happened to this instance, newest first: [{ts, action, by, detail}].
        Only instance actions (INSTANCE_ACTIONS) and only allow-listed detail keys, so
        nothing sealed and nothing about another user can come through."""
        r = self._instance_row(who, iid)
        limit = max(1, min(int(limit), 200))
        acts = sorted(INSTANCE_ACTIONS)
        rows = self.db.all(f"SELECT * FROM audit WHERE target=? AND ts>=? AND action IN ({','.join('?' * len(acts))}) "
                           f"ORDER BY id DESC LIMIT ?", (iid, r["created_at"], *acts, limit))
        out = []
        for a in rows:
            try:
                detail = json.loads(a["detail"] or "{}")
            except ValueError:
                detail = {}
            by = "you" if a["actor"] == who.user_id else "system" if a["actor"] in SYSTEM_ACTORS else "admin"
            out.append({"ts": a["ts"], "action": a["action"], "by": by,
                        "detail": {k: v for k, v in detail.items() if k in ACTIVITY_DETAIL_KEYS and v not in (None, "", [])}})
        return out

    def stop_instance(self, who: Principal, iid: str) -> dict:
        r = self._instance_row(who, iid)
        if r["status"] not in ("running", "failed"):
            raise InvalidState(f"cannot stop an instance that is {r['status']}")
        report = stop_instance(self.fly, self._naming(r), machines=self._machines_for(r))
        self._set(iid, status="stopped" if report.ok else "failed", error="" if report.ok else str(report.failed)[:300])
        self.db.audit(who.user_id, "stop_instance", iid)
        return self.get_instance(who, iid)

    def start_instance(self, who: Principal, iid: str) -> dict:
        r = self._instance_row(who, iid)
        if r["status"] not in ("stopped", "failed"):
            raise InvalidState(f"cannot start an instance that is {r['status']}")
        report = start_instance(self.fly, self._naming(r), machines=self._machines_for(r))
        self._set(iid, status="running" if report.ok else "failed", error="" if report.ok else str(report.failed)[:300])
        self.db.audit(who.user_id, "start_instance", iid)
        return self.get_instance(who, iid)

    def destroy_instance(self, who: Principal, iid: str) -> dict:
        r = self._instance_row(who, iid, allow_admin=True)
        return self._destroy(r, actor=who.user_id)

    def _destroy(self, r: dict, actor: str) -> dict:
        if r["status"] in ("destroyed",):
            return self._public(r)
        # No intermediate status on purpose: if the process dies mid-teardown the
        # instance still looks live and past its time, so the reaper simply retries.
        report = destroy_instance(self.fly, self._naming(r), machines=self._machines_for(r))
        if report.ok:
            self._set(r["id"], status="destroyed", urls="{}")
            delete_state(self.db, r["id"])
            self.db.audit(actor, "destroy_instance", r["id"])
        else:
            self._set(r["id"], status="failed", error=("could not destroy: " + str(report.failed))[:500])
            self.db.audit(actor, "destroy_failed", r["id"], failed=[a for a, _ in report.failed])
        return self._public(self.db.one("SELECT * FROM instances WHERE id=?", (r["id"],)))

    def reset_instance(self, who: Principal, iid: str) -> dict:
        """Wipe the instance's data: stop it, destroy the Mongo machine and volume,
        redeploy. Needs no credential inside the instance (so no env-admin), only the
        service's own org credential. State (secrets, PKI) is kept, so the redeploy is
        a real redeploy onto a fresh volume."""
        sealer = self._sealer(who)         # redeploying needs the sealed spec and the secrets
        r = self._instance_row(who, iid)
        if r["status"] not in ("running", "stopped", "failed"):
            raise InvalidState(f"cannot reset an instance that is {r['status']}")
        naming = self._naming(r)
        if naming.single_machine:
            # In place: drop the databases inside the mongodb container, restart
            # the one machine, register the issuer and verifier again.
            self._set(iid, status="resetting", error="")
            self.db.audit(who.user_id, "reset_instance", iid)
            self.runner.submit(self._reset_single_job, iid, sealer)
            return self.get_instance(who, iid)
        app = naming.app("mongodb")
        self._set(iid, status="resetting", error="")
        stop_instance(self.fly, naming)
        if self.fly.app_exists(app):
            self.fly.destroy_machines(app)
            for v in self.fly.list_volumes(app):
                if v.get("state") != "destroyed":
                    self.fly.destroy_volume(app, v["id"])
        self.db.audit(who.user_id, "reset_instance", iid)
        self.runner.submit(self._deploy_job, iid, sealer)
        return self.get_instance(who, iid)

    def _reset_single_job(self, iid: str, sealer):
        r = self.db.one("SELECT * FROM instances WHERE id=?", (iid,))
        if not r or r["status"] != "resetting":
            return
        naming = self._naming(r)
        report = reset_single_machine(self.fly, self.machines, naming)
        if not report.ok:
            self._set(iid, status="failed", error=("reset failed: " + str(report.failed))[:500])
            self.db.audit("system", "reset_failed", iid)
            return
        try:
            if self.register is not None:
                spec = self._spec(r, sealer)
                token = SealedStateStore(self.db, sealer, r["owner"]).load(iid).get("adminToken", b"").decode().strip()
                register_vc_services(naming, token, self.register, lambda m: None,
                                     admin_url=f"https://{naming.machine_app()}.fly.dev" if spec.public_ips else "")
            self._set(iid, status="running", error="")
            self.db.audit("system", "reset_done", iid)
        except (DeployError, VaultError) as e:
            self._set(iid, status="failed", error=str(e)[:500])
        except Exception as e:                              # noqa: BLE001
            log.exception("reset of %s failed unexpectedly", iid)
            self._set(iid, status="failed", error=f"internal error during reset ({type(e).__name__}: {e})"[:500])

    def set_keep(self, who: Principal, iid: str, keep: bool) -> dict:
        r = self._instance_row(who, iid)
        user = self._user(who.user_id)
        if keep and not r["kept"]:
            with self.db.transaction() as d:
                self._check_keep(d, user)
                d.execute("UPDATE instances SET kept=1, expires_at=NULL, updated_at=? WHERE id=?", (self.clock(), iid))
        elif not keep and r["kept"]:
            self._set(iid, kept=0, expires_at=self.clock() + self.limits.ttl_days * DAY)
        self.db.audit(who.user_id, "set_keep", iid, keep=keep)
        return self.get_instance(who, iid)

    # ---- background work --------------------------------------------------------

    def reap(self) -> list:
        """Destroy instances whose time is up. Returns their ids."""
        now = self.clock()
        due = self.db.all(f"SELECT * FROM instances WHERE expires_at IS NOT NULL AND expires_at<=? "
                          f"AND status IN ({','.join('?' * len(LIVE))})", (now, *LIVE))
        for r in due:
            self._destroy(r, actor="reaper")
        return [r["id"] for r in due]

    def lapse_kept(self) -> list:
        """A kept instance whose owner's allowance ran out (or whose owner is
        disabled) goes back on the normal expiry clock, with a day's grace - never
        an instant deletion."""
        now = self.clock()
        rows = self.db.all(
            "SELECT i.id FROM instances i JOIN users u ON u.id=i.owner WHERE i.kept=1 AND i.status!='destroyed' "
            "AND ((u.kept_until IS NOT NULL AND u.kept_until<?) OR u.disabled=1)", (now,))
        for r in rows:
            self._set(r["id"], kept=0, expires_at=now + self.limits.lapse_grace_days * DAY)
            self.db.audit("system", "keep_lapsed", r["id"])
        return [r["id"] for r in rows]

    def _app_pattern(self):
        comps = "|".join(re.escape(c) for c in sorted(component_names(), key=len, reverse=True))
        return re.compile(rf"^{re.escape(self.platform.app_prefix)}-([a-z2-7]{{{ID_LENGTH}}})-({comps})$")

    def _machine_app_pattern(self):
        """A single-machine instance's one app: <prefix>-<8 id chars>, nothing after."""
        return re.compile(rf"^{re.escape(self.platform.app_prefix)}-([a-z2-7]{{{ID_LENGTH}}})$")

    def sweep_orphans(self) -> dict:
        """Find apps in the org that look like ours but belong to no live instance and
        destroy them after a grace period. Only apps matching exactly
        <prefix>-<8 id chars>-<known component> (the apps layout) or
        <prefix>-<8 id chars> (the single-machine layout's one app) are ever
        considered, so anything else in the org is invisible to this. Both shapes
        are recognised whatever the platform deploys now, so switching layouts
        never strands an instance."""
        now = self.clock()
        pat, single = self._app_pattern(), self._machine_app_pattern()
        live = {r["id"] for r in self.db.all(f"SELECT id FROM instances WHERE status IN ({','.join('?' * len(LIVE))})", LIVE)}
        found, layouts = {}, {}
        for app in self.fly.list_apps():
            m = pat.match(app) or single.match(app)
            if m and m.group(1) not in live:
                found.setdefault(m.group(1), []).append(app)
                layouts.setdefault(m.group(1), set()).add("apps" if m.re is pat else LAYOUT_SINGLE_MACHINE)
        seen = {r["app"]: r["first_seen"] for r in self.db.all("SELECT * FROM orphans")}
        current = {a for apps in found.values() for a in apps}
        for gone in set(seen) - current:
            self.db.execute("DELETE FROM orphans WHERE app=?", (gone,))
        destroyed = []
        for iid, apps in found.items():
            for a in apps:
                self.db.execute("INSERT OR IGNORE INTO orphans(app, first_seen) VALUES(?,?)", (a, now))
            first = min(self.db.one("SELECT first_seen FROM orphans WHERE app=?", (a,))["first_seen"] for a in apps)
            if now - first >= self.limits.sweep_grace_seconds:
                ok = True
                for layout in sorted(layouts[iid]):
                    naming = Naming(iid, app_prefix=self.platform.app_prefix, layout=layout)
                    ok = destroy_instance(self.fly, naming,
                                          machines=self.machines if naming.single_machine else None).ok and ok
                self.db.audit("sweeper", "destroy_orphan", iid, apps=apps, ok=ok)
                if ok:
                    destroyed.append(iid)
                    for a in apps:
                        self.db.execute("DELETE FROM orphans WHERE app=?", (a,))
        return {"orphans": sorted(found), "destroyed": destroyed}

    def tick(self) -> dict:
        """One pass of all periodic work; call it from a timer."""
        return {"lapsed": self.lapse_kept(), "reaped": self.reap(), "sweep": self.sweep_orphans()}
