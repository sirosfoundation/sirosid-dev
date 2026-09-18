#!/usr/bin/env python3
"""vc-apigw's datastore API from outside an environment, authenticated.

    python3 scripts/datastore.py token  [--env NAME]
    python3 scripts/datastore.py search --scope iban_ov [--env NAME]
    python3 scripts/datastore.py upload fixtures/vc-bootstrapping/iban_ov.json [--env NAME]
    python3 scripts/datastore.py sync [--dry-run] [--scope NAME] [--env NAME]

Without --env the target is the local compose stack (fixtures/rendered/
vc-apigw.yaml, key in fixtures/rendered-secrets/); with it, the named Fly
environment (fixtures/rendered/fly-<env>/, its own key). Everything the token
has to match - issuer, audience, the SPOCP subject - is read from that
rendered apigw config rather than duplicated here, so a chart or values
change cannot silently desynchronise the caller from the server.

`upload` exists because vc-apigw imports its bootstrapping documents only
into an EMPTY datastore: a new datastore-sourced credential type reaches an
environment that already holds data either through a wipe or through this.
A bootstrapping file is already in the bulk endpoint's request shape.

`sync` is the same idea taken to the whole fixture set: it makes the
datastore and the identity mappings say exactly what the repository says,
adding, replacing and removing, so an environment that has been running for
weeks can be brought back in step without the wipe that would also take the
wallet-backend databases with it.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

import api_auth
import fly_common

ROOT = Path(__file__).resolve().parent.parent


class Target:
    def __init__(self, env: str = None, url: str = None):
        self.env = env
        if env:
            self.rendered = ROOT / "fixtures" / "rendered" / f"fly-{env}"
            self.key = self.rendered / api_auth.KEY_FILENAME
            self.url = url or fly_common.app_url(env, "vc-apigw")
        else:
            self.rendered = ROOT / "fixtures" / "rendered"
            self.key = ROOT / "fixtures" / "rendered-secrets" / api_auth.KEY_FILENAME
            self.url = url
        config_path = self.rendered / "vc-apigw.yaml"
        if not config_path.exists() or not self.key.exists():
            where = f"make fly-up ENV={env}" if env else "make up VC=yes"
            sys.exit(f"{config_path} or {self.key} missing - render this target first ({where})")
        config = yaml.safe_load(config_path.read_text())
        api_server = config["apigw"]["api_server"]
        self.auth = api_server["api_auth"]["jwks"]
        self.subject = spocp_subject(api_server["api_auth"].get("rules") or [])
        if not self.url:
            # The compose stack publishes apigw on the host at the same port
            # its public URL names; the hostname itself (vc-apigw.localhost)
            # need not resolve on the caller's machine.
            port = urllib.parse.urlsplit(config["apigw"]["public_url"]).port or 80
            self.url = f"http://localhost:{port}"

    def token(self) -> str:
        return api_auth.mint(self.key, self.auth["issuer"], self.auth["audience"], self.subject)

    def request(self, method: str, path: str, body=None, query: dict = None):
        url = self.url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token()}",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code == 409:
                sys.exit(f"{method} {url}: HTTP 409 - at least one of these document_ids is already in "
                         f"the datastore (the bulk upload is all-or-nothing); check with `search` first. {detail}")
            if e.code == 401:
                sys.exit(f"{method} {url}: HTTP 401 - the token was not accepted. The environment's apigw "
                         f"verifies against the JWKS rendered from {self.key}; if it was deployed from another "
                         f"checkout, that key differs from yours. {detail}")
            sys.exit(f"{method} {url}: HTTP {e.code} {detail}")
        return json.loads(raw) if raw else None


def spocp_subject(rules: list) -> str:
    """The subject the chart's admin rule grants /api/v1/* to - `admin@<tenant.id>`."""
    for rule in rules:
        m = re.search(r"\(subject ([^)*]+)\)", rule)
        if m and "/api/v1/" in rule:
            return m.group(1)
    sys.exit("no SPOCP rule with a concrete subject for /api/v1/* in the rendered api_auth block")


def offer(target: "Target", scope: str, document_id: str = None, authentic_source: str = None,
          qr: bool = False) -> None:
    """A pre-authorized credential offer for one datastore document.

    This is what cross-device issuance is made of: the offer carries its own
    pre-authorized code, so the wallet that scans it needs no browser session
    and no OIDC login on the device holding it. It works for the
    PID-authenticated types too - the document is chosen here rather than
    resolved from a presented PID.
    """
    docs = target.request("GET", "/api/v1/datastore/search",
                          query={"scope": scope, "limit": 200})["data"]
    if not docs:
        sys.exit(f"no documents in scope {scope} - `search` to see what is there")
    if document_id:
        match = [d for d in docs if d["meta"]["document_id"] == document_id]
        if not match:
            sys.exit(f"{document_id} is not in scope {scope}; it has: "
                     + ", ".join(d["meta"]["document_id"] for d in docs))
    elif len(docs) == 1:
        match = docs
    else:
        match = [d for d in docs if d["meta"]["document_id"].endswith("-full")]
        if not match:
            sys.exit(f"scope {scope} has {len(docs)} documents and none marked -full; "
                     f"name one with --document-id")
    doc = match[0]
    meta = doc["meta"]

    reply = target.request("POST", "/api/v1/datastore/preauth_offer", body={
        "authentic_source": authentic_source or meta["authentic_source"],
        "scope": scope,
        "document_id": meta["document_id"]})
    url = reply["credential_offer_url"]
    print(f"{scope} / {meta['document_id']} ({meta['authentic_source']})", file=sys.stderr)
    print(url)
    if qr:
        import subprocess
        try:
            subprocess.run(["qrencode", "-t", "UTF8", url], check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f"(no QR: {e})", file=sys.stderr)


def sync_identity_mappings(target: "Target", path: Path, dry_run: bool = False) -> None:
    """PID-authenticated issuance resolves the holder through these, so a
    mapping that lags the fixtures fails with "no documents" - pointing at the
    documents, which are fine. Imported only into an empty datastore, like
    them."""
    wanted = {m["authentic_source_person_id"]: m
              for entries in json.loads(path.read_text()).values() for m in entries}
    have = {m["authentic_source_person_id"]: m for m in
            target.request("GET", "/api/v1/identity/mapping/search", query={"limit": 1000})["data"]}

    add = [k for k in wanted if k not in have]
    changed = [k for k in wanted
               if k in have and have[k].get("attributes") != wanted[k]["attributes"]]
    for person_id in add:
        print(f"  + mapping   {person_id}")
    for person_id in changed:
        print(f"  ~ mapping   {person_id}")
    if dry_run or not (add or changed):
        return
    for person_id in changed:
        m = wanted[person_id]
        target.request("PUT", "/api/v1/identity/mapping", body={
            "authentic_source": m["authentic_source"],
            "authentic_source_person_id": person_id,
            "attributes": m["attributes"]})
    for person_id in add:
        m = wanted[person_id]
        target.request("POST", "/api/v1/identity/mapping", body={
            "authentic_source": m["authentic_source"],
            "authentic_source_person_id": person_id,
            "attributes": m["attributes"]})
    print(f"mappings: {len(add)} added, {len(changed)} updated")


def sync(target: "Target", fixtures: Path, scopes: list = None, dry_run: bool = False) -> None:
    """Make the datastore hold exactly what the fixtures say.

    vc-apigw imports bootstrapping documents only into an EMPTY datastore, so
    a redeploy never updates an environment that already holds data - the
    documents drift from the repository and nothing says so. This closes that
    gap without clearing the environment, which would take the wallet-backend
    databases (real users, real credentials) with it.
    """
    mappings = fixtures / "identity_mappings.json"
    # Narrowing to some scopes leaves the mappings alone: they are shared by
    # every scope, so a partial view is not something to reconcile against.
    if not scopes and mappings.exists():
        sync_identity_mappings(target, mappings, dry_run)

    wanted = {}
    for path in sorted(fixtures.glob("*.json")):
        if path.stem == "identity_mappings":
            continue
        if scopes and path.stem not in scopes:
            continue
        for holder, doc in json.loads(path.read_text()).items():
            wanted[(doc["meta"]["scope"], doc["meta"]["document_id"])] = (holder, doc)

    have = {}
    for d in target.request("GET", "/api/v1/datastore/search", query={"limit": 1000})["data"]:
        meta = d["meta"]
        if scopes and meta["scope"] not in scopes:
            continue
        have[(meta["scope"], meta["document_id"])] = d

    add = [k for k in wanted if k not in have]
    gone = [k for k in have if k not in wanted]
    same = [k for k in wanted if k in have]
    changed = [k for k in same
               if have[k].get("document_data") != wanted[k][1]["document_data"]
               or have[k].get("identity_mapping_ids") != wanted[k][1]["identity_mapping_ids"]]

    print(f"{len(add)} to add, {len(changed)} to replace, {len(gone)} to remove, "
          f"{len(same) - len(changed)} already current")
    for scope, doc_id in add:
        print(f"  + {scope:10} {doc_id}")
    for scope, doc_id in changed:
        print(f"  ~ {scope:10} {doc_id}")
    for scope, doc_id in gone:
        print(f"  - {scope:10} {doc_id}")
    if dry_run:
        print("(dry run, nothing sent)")
        return

    for key in changed:
        _, doc = wanted[key]
        target.request("PUT", "/api/v1/datastore", body=doc)
    # The bulk body is a map keyed by holder, so it can only carry one
    # document per holder - which a bootstrapping file, one scope per file,
    # always satisfies. Send one call per scope for the same reason.
    for scope in dict.fromkeys(scope for scope, _ in add):
        batch = {wanted[k][0]: wanted[k][1] for k in add if k[0] == scope}
        target.request("POST", "/api/v1/datastore/bulk", body={"documents": batch})
    for scope, doc_id in gone:
        meta = have[(scope, doc_id)]["meta"]
        target.request("DELETE", "/api/v1/datastore",
                       body={"authentic_source": meta["authentic_source"], "scope": scope, "document_id": doc_id})
    print(f"done: {len(add)} added, {len(changed)} replaced, {len(gone)} removed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", help="named Fly environment (default: the local compose stack)")
    parser.add_argument("--url", help="override the apigw base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token", help="print a fresh admin Bearer token (valid %d s)" % api_auth.DEFAULT_TTL)
    p = sub.add_parser("search", help="list documents")
    p.add_argument("--scope")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("upload", help="bulk-upload a fixtures/vc-bootstrapping/<scope>.json file")
    p.add_argument("file", type=Path)
    p = sub.add_parser("offer", help="mint a pre-authorized credential offer for one document (cross-device issuance)")
    p.add_argument("--scope", required=True)
    p.add_argument("--document-id", help="default: the scope's only document, or its -full one")
    p.add_argument("--authentic-source", help="default: read from the document")
    p.add_argument("--qr", action="store_true", help="also print the offer as a QR code (needs qrencode)")
    p = sub.add_parser("sync", help="make the datastore match fixtures/vc-bootstrapping (add, replace, remove)")
    p.add_argument("--dir", type=Path, default=Path("fixtures/vc-bootstrapping"))
    p.add_argument("--scope", action="append", help="limit to these scopes (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    target = Target(args.env, args.url)
    if args.cmd == "token":
        print(target.token())
    elif args.cmd == "search":
        docs = target.request("GET", "/api/v1/datastore/search", query={"scope": args.scope, "limit": args.limit})["data"]
        for d in docs:
            print(f"{d['meta']['scope']:12} {d['meta']['document_id']:45} {','.join(d.get('identity_mapping_ids', []))}")
        print(f"{len(docs)} document(s)", file=sys.stderr)
    elif args.cmd == "offer":
        offer(target, args.scope, args.document_id, args.authentic_source, args.qr)
    elif args.cmd == "sync":
        sync(target, args.dir, args.scope, args.dry_run)
    elif args.cmd == "upload":
        documents = json.loads(args.file.read_text())
        reply = target.request("POST", "/api/v1/datastore/bulk", body={"documents": documents})
        print(f"uploaded {reply.get('count')} document(s) from {args.file} to {target.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
