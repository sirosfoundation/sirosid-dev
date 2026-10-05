#!/usr/bin/env python3
"""Rotate the control plane's Fly org token (its FLY_API_TOKEN secret).

    FLY_ROTATOR_TOKEN=... [FLY_APP_TOKEN=...] python3 scripts/rotate_fly_token.py \
        --org sirosdev --app sirosid-console \
        --health-url https://console.sirosid.dev/healthz [--dry-run]

Why it runs as a separate identity: an org token cannot mint org tokens (nor app
deploy tokens), so the service cannot rotate its own credential. FLY_ROTATOR_TOKEN
belongs to a bot Fly USER who is a member of the target org (--org) only. The
secret is set on --app with FLY_APP_TOKEN when given - an app-scoped deploy token,
needed when the app lives in another org than the one whose token it holds (the
console runs in sirosfoundation, its instances in sirosdev) - else with the rotator
token. deploy/control-plane/README.md has the setup.

Order, and what each failure leaves behind:
  1. list the org's unrevoked tokens called --name     (these are the "old" ones)
  2. mint a new org token called --name, bounded expiry  -> fails: nothing changed
  3. stage it as the app's FLY_API_TOKEN (stdin, never argv) and deploy the staged
     secret                                               -> fails: the NEW token is
     revoked again (it was never in use), old ones kept
  4. wait until --health-url answers {"ok": true}         -> fails: nothing revoked;
     both tokens stay valid and a human looks (exit 3)
  5. revoke exactly the ids listed in step 1             -> never the new one
The token is never printed: every line of output goes through redact().

--dry-run does step 1 and says what it would do. Exit codes: 0 ok, 1 usage or
listing problem, 2 mint/set failed (nothing revoked), 3 unhealthy (nothing revoked),
4 revocation failed (the new token is live; revoke the listed ids by hand).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core.fly import FlyClient, FlyError  # noqa: E402

DEFAULT_NAME = "sirosid-console"
SECRET = "FLY_API_TOKEN"
_TOKENISH = re.compile(r"(FlyV1\s+\S+|fm[12]_[A-Za-z0-9+/=_,-]+|fo1_[A-Za-z0-9_-]+)")


def redact(text: str, *secrets: str) -> str:
    """Remove every given secret and anything shaped like a Fly token."""
    text = str(text)
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return _TOKENISH.sub("<redacted>", text)


def parse_tokens(text: str) -> list:
    """`flyctl tokens list -o <org> -s org` -> [{"id", "name", "revoked_at"}].

    flyctl has no --json here and keeps listing revoked tokens, so the columns are
    found by their header (ID | NAME | CREATED BY | EXPIRES AT | REVOKED AT), not by
    position. The row logic is FlyClient.parse_token_table's."""
    header = None
    rows = []
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        if "│" not in line:
            continue
        cols = [c.strip() for c in line.split("│")]
        if cols[0] == "ID":
            header = [c.upper() for c in cols]
            continue
        if not cols[0] or set(cols[0]) <= set("─┼├┤ "):
            continue
        if header is None:
            raise ValueError("token table without a header row")
        row = dict(zip(header, cols + [""] * (len(header) - len(cols))))
        rows.append({"id": row["ID"], "name": row.get("NAME", ""), "revoked_at": row.get("REVOKED AT", "")})
    if header is not None and "REVOKED AT" not in header:
        # Without the column every revoked token would look live and be "revoked" again,
        # harmless - but it also means the format changed under us. Refuse rather than guess.
        raise ValueError("token table has no REVOKED AT column; flyctl's output format changed")
    return rows


def http_healthy(url: str, timeout: float = 10) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200 and json.loads(r.read().decode() or "{}").get("ok") is True
    except Exception:                                       # noqa: BLE001 - any failure is "not healthy"
        return False


def wait_healthy(check, url, timeout: float, interval: float, consecutive: int, sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline, ok = clock() + timeout, 0
    while clock() < deadline:
        ok = ok + 1 if check(url) else 0
        if ok >= consecutive:
            return True
        sleep(interval)
    return False


class Rotation:
    def __init__(self, fly: FlyClient, *, org, app, name=DEFAULT_NAME, expiry="1440h", health_url,
                 health=http_healthy, health_timeout=300.0, health_interval=5.0, health_consecutive=3,
                 out=print, sleep=time.sleep, clock=time.monotonic, app_fly: FlyClient = None):
        # `fly` lists/mints/revokes org tokens in `org`; `app_fly` sets the secret on
        # `app` (which may live in ANOTHER org, e.g. the console in sirosfoundation
        # while its instances live in sirosdev). Default: the same identity.
        self.app_fly = app_fly or fly
        self.fly, self.org, self.app, self.name, self.expiry = fly, org, app, name, expiry
        self.health_url, self.health = health_url, health
        self.health_timeout, self.health_interval, self.health_consecutive = health_timeout, health_interval, health_consecutive
        self._out, self.sleep, self.clock = out, sleep, clock
        self._new = ""
        # FlyClient traces commands and, on a failure, echoes flyctl's own output -
        # which could quote the value it was given. Everything it says goes through
        # redact() too, against the token minted in THIS run as well as by shape.
        for client in {id(fly): fly, id(self.app_fly): self.app_fly}.values():
            raw_out, raw_err = client._out, client._err
            client._out = (lambda o: lambda m: o(redact(m, self._new)))(raw_out)
            client._err = (lambda e: lambda m: e(redact(m, self._new)))(raw_err)

    def say(self, msg):
        self._out(redact(msg, self._new))

    def old_tokens(self) -> list:
        r = self.fly.run("tokens", "list", "-o", self.org, "-s", "org", capture=True)
        return [t for t in parse_tokens(r.stdout or "") if t["name"] == self.name and not t["revoked_at"]]

    def _mint(self) -> str:
        r = self.fly.run("tokens", "create", "org", "-o", self.org, "-n", self.name, "-x", self.expiry, "--json",
                         check=False, capture=True)
        if r.returncode != 0:
            lines = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
            raise FlyError(f"could not create an org token (exit {r.returncode}): " + redact(lines[-1][:240] if lines else ""))
        try:
            token = (json.loads(r.stdout or "{}") or {}).get("token") or ""
        except ValueError:
            token = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
        if not token.startswith("FlyV1 ") and token.startswith("fm2_"):
            token = "FlyV1 " + token
        if not token:
            raise FlyError("flyctl created a token but printed none")
        return token

    def _revoke_new(self, before_ids):
        """The new token was never in use: take it back. Its id is whatever unrevoked
        token with our name was not there before."""
        try:
            fresh = [t["id"] for t in self.old_tokens() if t["id"] not in before_ids]
            if len(fresh) == 1:
                self.fly.run("tokens", "revoke", fresh[0], check=False)
                self.say(f"revoked the unused new token {fresh[0]}")
            else:
                self.say(f"could not single out the new token ({len(fresh)} candidates); revoke it by hand")
        except (FlyError, ValueError) as e:
            self.say(f"could not revoke the unused new token: {e}")

    def run(self, dry_run=False) -> int:
        try:
            old = self.old_tokens()
        except (FlyError, ValueError) as e:
            self.say(f"cannot list the org's tokens: {e}")
            return 1
        ids = [t["id"] for t in old]
        self.say(f"{len(ids)} unrevoked org token(s) named {self.name!r} in {self.org}: {', '.join(ids) or '-'}")
        if dry_run:
            self.say(f"dry run: would mint a new {self.expiry} org token {self.name!r}, set it as {SECRET} on "
                     f"{self.app}, wait for {self.health_url}, then revoke {len(ids)} token(s)")
            return 0
        try:
            self._new = self._mint()
        except FlyError as e:
            self.say(f"minting failed, nothing changed: {e}")
            return 2
        self.say(f"minted a new org token {self.name!r} (expires in {self.expiry})")
        try:
            self.app_fly.import_secrets(self.app, {SECRET: self._new}, stage=True)
            self.app_fly.run("secrets", "deploy", "-a", self.app, capture=True)
        except FlyError as e:
            self.say(f"setting {SECRET} on {self.app} failed: {e}")
            self._revoke_new(set(ids))
            return 2
        self.say(f"{SECRET} set and deployed on {self.app}; waiting for {self.health_url}")
        if not wait_healthy(self.health, self.health_url, self.health_timeout, self.health_interval,
                            self.health_consecutive, sleep=self.sleep, clock=self.clock):
            self.say(f"{self.app} did not become healthy: NOT revoking the old token(s) {', '.join(ids) or '-'}; "
                     "both are valid - investigate, then revoke by hand")
            return 3
        if not ids:
            self.say("healthy; no old token to revoke")
            return 0
        r = self.fly.run("tokens", "revoke", *ids, check=False, capture=True)
        if r.returncode != 0:
            self.say(f"revoking {', '.join(ids)} failed (exit {r.returncode}); the new token is live - revoke them by hand")
            return 4
        try:
            still = [t["id"] for t in self.old_tokens() if t["id"] in ids]
        except (FlyError, ValueError):
            still = []
        if still:
            self.say(f"flyctl said ok but {', '.join(still)} still unrevoked - revoke by hand")
            return 4
        self.say(f"healthy; revoked {', '.join(ids)}")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--org", required=True)
    p.add_argument("--app", required=True)
    p.add_argument("--health-url", required=True)
    p.add_argument("--name", default=DEFAULT_NAME, help="token name; only tokens with this name are ever revoked")
    p.add_argument("--expiry", default="1440h", help="lifetime of the new token (default 60 days, rotated monthly)")
    p.add_argument("--health-timeout", type=float, default=300)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    token = os.environ.get("FLY_ROTATOR_TOKEN", "")
    if not token:
        print("FLY_ROTATOR_TOKEN is required (a bot user's token, see deploy/control-plane/README.md)", file=sys.stderr)
        return 1
    app_token = os.environ.get("FLY_APP_TOKEN", "")
    trace = lambda m: print(redact(m, token, app_token), file=sys.stderr) if str(m).strip() else None
    fly = FlyClient(org=a.org, token=token, out=trace, err=trace)
    app_fly = FlyClient(org=a.org, token=app_token, out=trace, err=trace) if app_token else None
    return Rotation(fly, org=a.org, app=a.app, name=a.name, expiry=a.expiry, health_url=a.health_url,
                    health_timeout=a.health_timeout, app_fly=app_fly,
                    out=lambda m: print(redact(m, token, app_token))).run(dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
