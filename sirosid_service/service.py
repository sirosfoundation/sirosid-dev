"""ControlPlane - every rule about who may do what to which instance.

Front ends (HTTP, MCP, a CLI) authenticate a caller and obtain a Principal; they
call these methods and nothing else. All ownership, quota, capability and policy
checks live here so no front end can forget one.

Fly is reached only through sirosid_core's FlyClient, in the service's own sandbox
org with its own credential. Slow work (deploy, reset) runs through an injected
Runner: threads in production, inline in tests.
"""
import json
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sirosid_core import policy as policy_mod
from sirosid_core import state as state_mod
from sirosid_core.components import component_names
from sirosid_core.deploy import DeployError, deploy_instance
from sirosid_core.fly import FlyClient, FlyError
from sirosid_core.lifecycle import destroy_instance, start_instance, stop_instance
from sirosid_core.naming import Naming
from sirosid_core.policy import PlatformPolicy, PolicyError
from sirosid_core.resources import Resources
from sirosid_core.spec import InstanceSpec

from .db import Database, DbStateStore, hash_token

DAY = 86400.0
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
ID_LENGTH = 8
# Ids that would read as an impersonation or an internal name. A generated id is
# random, so this only matters if the generator is ever swapped for a chosen one.
RESERVED_IDS = frozenset({"admin", "console", "status", "www", "api", "mail", "login", "siros", "sirosid", "support"})

LIVE = ("creating", "running", "stopped", "stopping", "starting", "resetting", "failed")


class ServiceError(Exception):
    """Base for errors a front end should show to the caller."""


class NotFound(ServiceError):
    pass


class Forbidden(ServiceError):
    pass


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
        self._pool.submit(fn, *args)


def generate_instance_id(rng=secrets) -> str:
    return "".join(rng.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


def _json(v):
    return json.dumps(v, sort_keys=True)


class ControlPlane:
    def __init__(self, db: Database, fly: FlyClient, resources: Resources, platform: PlatformPolicy = None,
                 limits: Limits = None, runner=None, register: Callable = None, clock=None,
                 id_generator: Callable[[], str] = None):
        self.db = db
        self.fly = fly
        self.resources = resources
        self.platform = platform or PlatformPolicy()
        self.limits = limits or Limits()
        self.runner = runner or SyncRunner()
        self.register = register
        self.clock = clock or db.clock
        self._new_id = id_generator or generate_instance_id
        self.store = DbStateStore(db)

    # ---- principals -------------------------------------------------------

    def principal_for(self, user_id: str) -> Principal:
        u = self._user(user_id)
        if u["disabled"]:
            raise Forbidden("this account is disabled")
        return Principal(u["id"], u["role"], frozenset(json.loads(u["capabilities"])))

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

    def validate_config(self, who: Principal, doc: dict) -> list:
        return [str(p) for p in policy_mod.validate(doc, who.capabilities, self.platform)]

    def save_config(self, who: Principal, name: str, doc: dict) -> dict:
        if not name or len(name) > 64:
            raise ServiceError("a config needs a name of at most 64 characters")
        problems = policy_mod.validate(doc, who.capabilities, self.platform)
        if problems:
            raise PolicyError(problems)
        now = self.clock()
        existing = self.db.one("SELECT id FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        if existing:
            self.db.execute("UPDATE configs SET doc=?, updated_at=? WHERE id=?", (_json(doc), now, existing["id"]))
            cid = existing["id"]
        else:
            if self.db.one("SELECT COUNT(*) AS n FROM configs WHERE owner=?", (who.user_id,))["n"] >= 50:
                raise QuotaExceeded("at most 50 saved configs")
            cid = "c_" + secrets.token_hex(6)
            self.db.execute("INSERT INTO configs(id,owner,name,doc,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                            (cid, who.user_id, name, _json(doc), now, now))
        self.db.audit(who.user_id, "save_config", cid, name=name)
        return {"id": cid, "name": name}

    def list_configs(self, who: Principal) -> list:
        return [{"id": r["id"], "name": r["name"], "updated_at": r["updated_at"]} for r in
                self.db.all("SELECT * FROM configs WHERE owner=? ORDER BY name", (who.user_id,))]

    def get_config(self, who: Principal, name: str) -> dict:
        r = self.db.one("SELECT * FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        if not r:
            raise NotFound("no such saved config")
        return json.loads(r["doc"])

    def delete_config(self, who: Principal, name: str):
        self.db.execute("DELETE FROM configs WHERE owner=? AND name=?", (who.user_id, name))
        self.db.audit(who.user_id, "delete_config", name)

    # ---- instances ------------------------------------------------------------

    def create_instance(self, who: Principal, *, config: dict = None, config_name: str = None, name: str = "",
                        keep: bool = False) -> dict:
        if config is None:
            config = self.get_config(who, config_name) if config_name else {}
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
            d.execute("INSERT INTO instances(id,owner,name,status,spec,config,created_at,updated_at,expires_at,kept) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (iid, who.user_id, name[:64], "creating", _json(spec.to_dict()), _json(config), self.clock(),
                       self.clock(), expires, 1 if keep else 0))
        self.db.audit(who.user_id, "create_instance", iid, kept=keep, images=sorted(spec.images))
        self.runner.submit(self._deploy_job, iid)
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
        return {"id": r["id"], "name": r["name"], "status": r["status"], "created_at": r["created_at"],
                "expires_at": r["expires_at"], "kept": bool(r["kept"]), "urls": json.loads(r["urls"]), "error": r["error"]}

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
        blobs = self.store.load(iid)
        token = blobs.get("adminToken", b"").decode().strip()
        return {"urls": json.loads(r["urls"]), "admin_token": token}

    def _set(self, iid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE instances SET {cols}, updated_at=? WHERE id=?", (*fields.values(), self.clock(), iid))

    def _spec(self, r) -> InstanceSpec:
        return InstanceSpec.from_dict(json.loads(r["spec"]))

    def _deploy_job(self, iid: str):
        r = self.db.one("SELECT * FROM instances WHERE id=?", (iid,))
        if not r or r["status"] not in ("creating", "resetting"):
            return
        spec = self._spec(r)
        try:
            with state_mod.workdir(self.store, iid, subdir=f"fly-{iid}") as scratch:
                result = deploy_instance(spec, self.fly, spec.naming(), self.resources, rendered_root=scratch,
                                         register=self.register, progress=None)
            self._set(iid, status="running", urls=_json(result.urls), error="")
            self.db.audit("system", "deployed", iid)
        except (DeployError, FlyError, policy_mod.PolicyError) as e:
            self._set(iid, status="failed", error=str(e)[:500])
            self.db.audit("system", "deploy_failed", iid, error=str(e)[:200])

    def stop_instance(self, who: Principal, iid: str) -> dict:
        r = self._instance_row(who, iid)
        if r["status"] not in ("running", "failed"):
            raise InvalidState(f"cannot stop an instance that is {r['status']}")
        report = stop_instance(self.fly, self._spec(r).naming())
        self._set(iid, status="stopped" if report.ok else "failed", error="" if report.ok else str(report.failed)[:300])
        self.db.audit(who.user_id, "stop_instance", iid)
        return self.get_instance(who, iid)

    def start_instance(self, who: Principal, iid: str) -> dict:
        r = self._instance_row(who, iid)
        if r["status"] not in ("stopped", "failed"):
            raise InvalidState(f"cannot start an instance that is {r['status']}")
        report = start_instance(self.fly, self._spec(r).naming())
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
        report = destroy_instance(self.fly, self._spec(r).naming())
        if report.ok:
            self._set(r["id"], status="destroyed", urls="{}")
            self.store.delete(r["id"])
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
        r = self._instance_row(who, iid)
        if r["status"] not in ("running", "stopped", "failed"):
            raise InvalidState(f"cannot reset an instance that is {r['status']}")
        naming = self._spec(r).naming()
        app = naming.app("mongodb")
        self._set(iid, status="resetting", error="")
        stop_instance(self.fly, naming)
        if self.fly.app_exists(app):
            self.fly.destroy_machines(app)
            for v in self.fly.list_volumes(app):
                if v.get("state") != "destroyed":
                    self.fly.destroy_volume(app, v["id"])
        self.db.audit(who.user_id, "reset_instance", iid)
        self.runner.submit(self._deploy_job, iid)
        return self.get_instance(who, iid)

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

    def sweep_orphans(self) -> dict:
        """Find apps in the org that look like ours but belong to no live instance and
        destroy them after a grace period. Only apps matching exactly
        <prefix>-<8 id chars>-<known component> are ever considered, so anything else
        in the org is invisible to this."""
        now = self.clock()
        pat = self._app_pattern()
        live = {r["id"] for r in self.db.all(f"SELECT id FROM instances WHERE status IN ({','.join('?' * len(LIVE))})", LIVE)}
        found = {}
        for app in self.fly.list_apps():
            m = pat.match(app)
            if m and m.group(1) not in live:
                found.setdefault(m.group(1), []).append(app)
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
                report = destroy_instance(self.fly, Naming(iid, app_prefix=self.platform.app_prefix))
                self.db.audit("sweeper", "destroy_orphan", iid, apps=apps, ok=report.ok)
                if report.ok:
                    destroyed.append(iid)
                    for a in apps:
                        self.db.execute("DELETE FROM orphans WHERE app=?", (a,))
        return {"orphans": sorted(found), "destroyed": destroyed}

    def tick(self) -> dict:
        """One pass of all periodic work; call it from a timer."""
        return {"lapsed": self.lapse_kept(), "reaped": self.reap(), "sweep": self.sweep_orphans()}
