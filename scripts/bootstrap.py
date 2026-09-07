#!/usr/bin/env python3
"""Register this environment's issuer and verifier with wallet-backend.

One implementation for the three places that need it, so a storage reset
reproduces exactly what a fresh deploy does:

  - `make up VC=yes` (was the Makefile's register-vc-services shell target)
  - `make fly-up` (scripts/fly-up.py)
  - env-admin's storage reset, after it has wiped Mongo and restarted the
    consumers (env-admin/server.py)

Why this exists at all: PDP's whitelist governs who is TRUSTED to issue and
verify; it does not populate the wallet's own list of available issuers and
verifiers. An environment with nothing registered looks completely healthy -
every health check green - and only fails when a user tries to add a
credential.

Idempotent. Drops any previously registered issuer whose identifier is no
longer the one apigw advertises (the identifier changes with the addressing
scheme - local vs TUNNELS vs Fly - and wallet-backend keys issuers by it, so
a stale entry would sit next to the new one and 502 on every Add Credentials
page load). Tenant "default" is go-wallet-backend's DefaultTenantID, created
by wallet-backend itself at startup; we wait for it rather than create it.

client_id is required, not cosmetic: without it wallet-backend falls back to
the OID4VCI "unregistered client" convention (client_id = redirect_uri), which
vc-apigw's static clients map never matches - every authorization_code
credential then fails PAR with invalid_client. "e2e-test-client" is the
client the rendered apigw config already carries.

Usage:
    python3 scripts/bootstrap.py --admin-url http://localhost:8081 --admin-token TOKEN \
        --issuer-url http://vc-apigw.localhost:9003 --verifier-url http://vc-verifier.localhost:9001
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_TENANT = "default"
ISSUER_CLIENT_ID = "e2e-test-client"


class BootstrapError(Exception):
    pass


def _request(method: str, url: str, token: str, body: dict | None = None, timeout: float = 10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def wait_for_tenant(admin_url: str, token: str, tenant: str = DEFAULT_TENANT, attempts: int = 30,
                    delay: float = 2.0, log=print) -> None:
    """wallet-backend creates the default tenant at startup; until it answers
    here, nothing below can succeed. Raises after `attempts` tries."""
    last = None
    for _ in range(attempts):
        try:
            status, _body = _request("GET", f"{admin_url}/admin/tenants/{tenant}", token)
            if status == 200:
                return
            last = f"HTTP {status}"
            if status in (401, 403):
                raise BootstrapError(f"admin token rejected by {admin_url} ({last}) - wrong ADMIN_TOKEN?")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = str(e)
        time.sleep(delay)
    raise BootstrapError(f"wallet-backend's admin API at {admin_url} never became ready ({last})")


def register(admin_url: str, token: str, issuer_url: str, verifier_url: str,
             tenant: str = DEFAULT_TENANT, verifier_name: str = "VC Verifier", log=print) -> dict:
    """Idempotently register issuer_url and verifier_url with the tenant.
    Returns a summary dict; raises BootstrapError on a hard failure."""
    admin_url = admin_url.rstrip("/")
    wait_for_tenant(admin_url, token, tenant, log=log)
    summary = {"removed_stale_issuers": [], "issuer": None, "verifier": None}

    status, existing = _request("GET", f"{admin_url}/admin/tenants/{tenant}/issuers", token)
    if status == 200 and isinstance(existing, dict):
        for issuer in existing.get("issuers", []) or []:
            if issuer.get("credential_issuer_identifier") != issuer_url and issuer.get("id"):
                st, _ = _request("DELETE", f"{admin_url}/admin/tenants/{tenant}/issuers/{issuer['id']}", token)
                if 200 <= st < 300:
                    summary["removed_stale_issuers"].append(issuer.get("credential_issuer_identifier"))
                    log(f"removed stale issuer registration {issuer.get('credential_issuer_identifier')}")

    status, _ = _request("POST", f"{admin_url}/admin/tenants/{tenant}/issuers", token, {
        "credential_issuer_identifier": issuer_url, "visible": True, "client_id": ISSUER_CLIENT_ID,
    })
    if 200 <= status < 300:
        summary["issuer"] = "registered"
    elif status == 409:
        summary["issuer"] = "already registered"
    else:
        raise BootstrapError(f"could not register issuer {issuer_url} (HTTP {status})")
    log(f"issuer {issuer_url}: {summary['issuer']}")

    status, _ = _request("POST", f"{admin_url}/admin/tenants/{tenant}/verifiers", token,
                         {"name": verifier_name, "url": verifier_url})
    if 200 <= status < 300:
        summary["verifier"] = "registered"
    elif status == 409:
        summary["verifier"] = "already registered"
    else:
        raise BootstrapError(f"could not register verifier {verifier_url} (HTTP {status})")
    log(f"verifier {verifier_url}: {summary['verifier']}")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--admin-token", required=True)
    parser.add_argument("--issuer-url", required=True, help="the credential_issuer identity vc-apigw advertises")
    parser.add_argument("--verifier-url", required=True, help="vc-verifier's public URL")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    args = parser.parse_args(argv)
    try:
        register(args.admin_url, args.admin_token, args.issuer_url, args.verifier_url, args.tenant)
    except BootstrapError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
