#!/usr/bin/env python3
"""Generate an Android-compatible vc-config from the base conformance config.

Patches:
  - oidc.issuer_url   → http://<gateway>:<oidc_port>   (reachable from Android browser)
  - oidc.redirect_uri → http://<gateway>:<proxy_port>/oidcrp/callback
  - Adds siros-sample://callback to e2e-test-client redirect_uris

The base vc-config.yaml stays untouched so conformance tests keep working.
"""
import argparse
import re
import sys
from pathlib import Path


def patch_config(
    base: str,
    gateway: str,
    proxy_port: int,
    oidc_port: int,
    redirect_uri: str,
) -> str:
    out = base

    # 1. Patch OIDC issuer_url (under auth_providers.oidc)
    out = re.sub(
        r'(issuer_url:\s*)"http://mini-oidc:\d+"',
        rf'\1"http://{gateway}:{oidc_port}"',
        out,
        count=1,
    )

    # 2. Patch OIDC redirect_uri (under auth_providers.oidc)
    out = re.sub(
        r'(redirect_uri:\s*)"https://vc-proxy:\d+/oidcrp/callback"',
        rf'\1"http://{gateway}:{proxy_port}/oidcrp/callback"',
        out,
        count=1,
    )

    # 3. Add siros-sample://callback to e2e-test-client redirect_uris
    #    Insert after the last existing redirect_uri line for e2e-test-client
    if redirect_uri and redirect_uri not in out:
        # Find the block: after the last "- ..." under e2e-test-client redirect_uri
        # We look for the pattern of redirect_uri entries followed by scopes
        out = re.sub(
            r'("e2e-test-client":\s*\n\s*type:\s*"public"\s*\n\s*redirect_uri:\s*\n(?:\s*-\s*"[^"]*"\s*\n)*)',
            lambda m: m.group(0).rstrip("\n") + f'\n            - "{redirect_uri}"\n',
            out,
            count=1,
        )

    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="fixtures/vc-config.yaml",
        help="Base config file (default: fixtures/vc-config.yaml)",
    )
    parser.add_argument(
        "--output",
        default="fixtures/vc-config-android.yaml",
        help="Output file (default: fixtures/vc-config-android.yaml)",
    )
    parser.add_argument("--gateway", default="192.168.240.1", help="Waydroid host gateway IP")
    parser.add_argument("--proxy-port", type=int, default=8091, help="Issuer proxy port")
    parser.add_argument("--oidc-port", type=int, default=9005, help="Mini-OIDC port on host")
    parser.add_argument(
        "--redirect-uri",
        default="siros-sample://callback",
        help="Custom scheme redirect URI to add",
    )
    args = parser.parse_args()

    base_path = Path(args.base)
    if not base_path.exists():
        print(f"Error: base config not found: {base_path}", file=sys.stderr)
        sys.exit(1)

    base = base_path.read_text()
    patched = patch_config(base, args.gateway, args.proxy_port, args.oidc_port, args.redirect_uri)

    out_path = Path(args.output)
    out_path.write_text(patched)
    print(f"Generated {out_path} from {base_path}")


if __name__ == "__main__":
    main()
