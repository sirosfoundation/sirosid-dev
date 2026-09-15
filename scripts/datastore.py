#!/usr/bin/env python3
"""vc-apigw's datastore API from outside an environment, authenticated.

    python3 scripts/datastore.py token  [--env NAME]
    python3 scripts/datastore.py search --scope iban_ov [--env NAME]
    python3 scripts/datastore.py upload fixtures/vc-bootstrapping/iban_ov.json [--env NAME]

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
    args = parser.parse_args(argv)

    target = Target(args.env, args.url)
    if args.cmd == "token":
        print(target.token())
    elif args.cmd == "search":
        docs = target.request("GET", "/api/v1/datastore/search", query={"scope": args.scope, "limit": args.limit})["data"]
        for d in docs:
            print(f"{d['meta']['scope']:12} {d['meta']['document_id']:45} {','.join(d.get('identity_mapping_ids', []))}")
        print(f"{len(docs)} document(s)", file=sys.stderr)
    elif args.cmd == "upload":
        documents = json.loads(args.file.read_text())
        reply = target.request("POST", "/api/v1/datastore/bulk", body={"documents": documents})
        print(f"uploaded {reply.get('count')} document(s) from {args.file} to {target.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
