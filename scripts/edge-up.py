#!/usr/bin/env python3
"""Deploy the shared EDGE app (sirosid_core/edge.py) into a Fly org.

    FLY_API_TOKEN=... python3 scripts/edge-up.py --org sirosdev --app sirosid-edge --domain sirosid.dev
    FLY_API_TOKEN=... python3 scripts/edge-up.py --org sirosdev --app sbx-edge-x1y2z3 --test-domain sm.invalid

The edge answers `<component>-<id>.<domain>` with fly-replay to app `sid-<id>`
and serves the apex/www static site from edge/site. It is created on the org's
DEFAULT network (cross-network replay is refused) with one shared IPv4 (and an
IPv6 unless --no-v6); never a dedicated IPv4.

Certificates are NOT requested: the script prints the DNS records and the
`flyctl certs add` commands to run once the domain's DNS points here. With
--test-domain (a synthetic domain nobody resolves) there is nothing to add -
test through https://<app>.fly.dev with a Host header instead.

The token is read from FLY_API_TOKEN only and handed to flyctl through its
environment; it is never written anywhere.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sirosid_core.edge import (EdgeConfigError, cert_commands, check_domain, check_edge_app,  # noqa: E402
                               dns_records, edge_fly_toml, edge_nginx_conf, render_site)
from sirosid_core.fly import FlyClient, FlyError  # noqa: E402

EDGE_DIR = ROOT / "edge"


TEXT_SUFFIXES = (".html", ".css", ".svg", ".txt")


def build_context(dest: Path, app: str, domain: str, region: str) -> Path:
    """Dockerfile + generated edge.conf + the rendered site + fly.toml, in `dest`."""
    shutil.copy(EDGE_DIR / "Dockerfile", dest / "Dockerfile")
    (dest / "edge.conf").write_text(edge_nginx_conf(domain))
    root = EDGE_DIR / "site"
    text_src, binary_src = {}, {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            # Nothing hidden is ever published, and above all nothing under /.well-known/ (see edge.py).
            raise SystemExit(f"edge/site/{rel}: hidden files and directories are not published")
        if p.suffix in TEXT_SUFFIXES:
            text_src[rel.as_posix()] = p.read_text()
        else:
            binary_src[rel.as_posix()] = p.read_bytes()
    partials = {p.stem: p.read_text() for p in sorted((EDGE_DIR / "partials").glob("*.html"))}
    for name, text in render_site(text_src, domain, partials).items():
        out = dest / "site" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    for name, data in binary_src.items():                    # images are shipped byte for byte
        out = dest / "site" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    (dest / "fly.toml").write_text(edge_fly_toml(app, region=region))
    return dest


def ensure_ips(fly: FlyClient, app: str, v6: bool) -> dict:
    """A SHARED v4 (and a v6) once - `ips allocate-v6` is not idempotent."""
    def listing():
        r = fly.run("ips", "list", "-a", app, "--json", check=False, capture=True)
        try:
            return json.loads(r.stdout or "[]") if r.returncode == 0 else []
        except ValueError:
            return []
    kinds = {str(ip.get("Type") or ip.get("type") or "").lower() for ip in listing()}
    if not any(k in ("shared_v4", "v4") for k in kinds):
        fly.run("ips", "allocate-v4", "--shared", "-a", app)
    if v6 and "v6" not in kinds:
        fly.run("ips", "allocate-v6", "-a", app)
    out = {}
    for ip in listing():
        kind = str(ip.get("Type") or ip.get("type") or "").lower()
        addr = ip.get("Address") or ip.get("address") or ""
        if kind in ("shared_v4", "v4") and addr:
            out["v4"] = addr
        elif kind == "v6" and addr:
            out["v6"] = addr
    if "v4" not in out:
        # A shared v4 is not always listed by `ips list`; the app's name resolves to it.
        r = fly.run("ips", "list", "-a", app, check=False, capture=True)
        for line in (r.stdout or "").splitlines():
            if "shared" in line.lower():
                for tok in line.split():
                    if tok.count(".") == 3:
                        out["v4"] = tok
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--org", required=True, help="Fly org to deploy the edge into")
    p.add_argument("--app", required=True, help="the edge's Fly app name (must not start with 'sid-')")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--domain", help="the real instances domain (e.g. sirosid.dev)")
    g.add_argument("--test-domain", help="a synthetic domain (e.g. sm.invalid): nothing to put in DNS")
    p.add_argument("--region", default="arn")
    p.add_argument("--no-v6", action="store_true", help="allocate only the shared IPv4")
    p.add_argument("--render-only", metavar="DIR", help="write the build context to DIR and stop")
    args = p.parse_args(argv)
    domain = args.domain or args.test_domain
    try:
        check_domain(domain)
        check_edge_app(args.app)
    except EdgeConfigError as e:
        raise SystemExit(str(e))

    if args.render_only:
        dest = Path(args.render_only)
        dest.mkdir(parents=True, exist_ok=True)
        build_context(dest, args.app, domain, args.region)
        print(f"build context for {args.app} ({domain}) written to {dest}")
        return

    token = os.environ.get("FLY_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("FLY_API_TOKEN is required (the target org's token)")
    fly = FlyClient(org=args.org, token=token)
    try:
        # Default network: Fly refuses to replay into another network.
        fly.ensure_app(args.app)
        ips = ensure_ips(fly, args.app, v6=not args.no_v6)
        with tempfile.TemporaryDirectory(prefix="sirosid-edge-") as tmp:
            ctx = build_context(Path(tmp), args.app, domain, args.region)
            local = shutil.which("docker") is not None
            fly.deploy(["deploy", str(ctx), "--config", str(ctx / "fly.toml"), "-a", args.app,
                        "--ha=false", "--no-public-ips", "--yes",
                        "--local-only" if local else "--remote-only"], cwd=ctx)
    except FlyError as e:
        raise SystemExit(str(e))

    print()
    print(f"=== Edge {args.app} is up: https://{args.app}.fly.dev ===")
    print(f"  replays <component>-<id>.{domain} to app sid-<id>; serves {domain} and www.{domain} itself")
    if args.test_domain:
        print(f"  --test-domain: nothing goes into DNS and no certificate is requested. Test with:")
        print(f"    curl -H 'Host: {domain}' https://{args.app}.fly.dev/")
        print(f"    curl -H 'Host: vc-apigw-<id>.{domain}' https://{args.app}.fly.dev/health")
        return
    print()
    print(f"DNS records for {domain} (the console's own record points at the console app, not here):")
    for name, kind, value in dns_records(domain, args.app, ips.get("v4", ""), ips.get("v6", "")):
        print(f"  {name:<28} {kind:<6} {value}")
    print()
    print("Then, once those resolve (NOT run by this script):")
    for cmd in cert_commands(domain, args.app):
        print(f"  {cmd}")
    print("  (each prints the _acme-challenge CNAME to add for DNS validation; the wildcard needs it)")


if __name__ == "__main__":
    main()
