#!/usr/bin/env python3
"""Render wallet-backend, go-trust (PDP) and the vc services' config from chart/.

The CLI for sirosid_core.render - see that module's docstring for the design,
the two targets (compose, fly) and what is and is not patched after helm runs.
`make render-helm-config` calls this.
"""
import argparse
import sys
from pathlib import Path

import yaml  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sirosid_core import render as _render  # noqa: E402
from sirosid_core.render import render  # noqa: E402,F401  (re-exported)
from sirosid_core.resources import Resources  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=_render.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chart-dir", default=str(SIROSID_DEV_ROOT / "chart"),
                         help="Path to the config chart (default: ./chart)")
    parser.add_argument("--target", choices=["compose", "fly"], default="compose")
    parser.add_argument("--env", help="Environment name, required for --target fly (e.g. test1)")
    parser.add_argument("--android-apk-key-hash", action="append", default=[],
                         help="base64url apk-key-hash (no padding) to add to wallet-backend's "
                              "rp_origins, on top of the production ones siros-id-stack already "
                              "carries - repeatable, applies to both --target compose and "
                              "--target fly. See scripts/android_apps.py (.android-apps / "
                              "--android-app) for the developer-facing (colon-hex) form of this "
                              "- the Makefile passes its output through here for both targets.")
    parser.add_argument("--mongo-password", default=None,
                         help="--target fly only: mongodb root password (fly-up.py generates and sets "
                              "this as a Fly secret on the mongodb app itself; passed here so "
                              "wallet-backend's config embeds a matching authenticated connection URI). "
                              "Omitting this does NOT error - it silently renders an UNAUTHENTICATED "
                              "mongo URI instead. Only safe within the same fly-up.py invocation that "
                              "set the matching Fly secret; for a one-off render, just re-run "
                              "'make fly-up ENV=<name>' instead.")
    parser.add_argument("--hostname", action="append", default=[], metavar="ROLE=HOST",
                        help="override one chart hostname, e.g. issuer=abc.trycloudflare.com. "
                             "Roles: issuer (the apigw), issuerRegistry, verifier, walletFrontend, "
                             "walletBackend. A host not in vc_render.COMPOSE_PLAIN_HTTP_HOSTS keeps "
                             "the chart's https:// scheme, which is what TUNNELS=yes wants.")
    parser.add_argument("--mini-oidc-url", default="",
                        help="the OIDC provider apigw authenticates users against; overrides "
                             "values-dev.yaml's compose default (Waydroid reaches it on a "
                             "different address than the host does).")
    parser.add_argument("--dc-api-enable", default="",
                        help="'true'/'false' to force the verifier's W3C Digital Credentials API "
                             "support on or off; empty leaves values-base.yaml's own setting. "
                             "Means the same thing on both targets.")
    parser.add_argument("--zk-circuits-source", action="append", default=[],
                        help="zk-circuits catalog mirror base URL, repeatable/comma-separated; "
                             "empty uses vc's own default.")
    parser.add_argument("--credential-registries", default="",
                        help="Comma-separated registry base URLs (the Makefile's "
                             "CREDENTIAL_REGISTRIES, passed only for REGISTRY=external). When set, "
                             "both vc and wallet-backend resolve credential metadata from these "
                             "registries instead of from documents vendored into this repo.")
    parser.add_argument("--env-values", action="store_true",
                        help="also layer environments/<env>.yaml's `values:` block (requires --env)")
    parser.add_argument("--conformance", action="store_true",
                         help="--target fly only: also whitelist the conformance suite's issuer/verifier "
                              "identity (https://sirosid-<env>-conformance.fly.dev/*) with PDP, so the "
                              "wallet accepts credential offers/presentation requests from it during "
                              "OID4VCI/OID4VP conformance testing - see build_fly_values_overlay().")
    parser.add_argument("--namespace", default="sirosid-dev")
    parser.add_argument("--out-dir", default=str(SIROSID_DEV_ROOT / "fixtures" / "rendered"))
    parser.add_argument("--secrets-dir", default=str(SIROSID_DEV_ROOT / "fixtures" / "rendered-secrets"))
    args = parser.parse_args()

    if args.target == "fly" and not args.env:
        parser.error("--target fly requires --env <name>")

    chart_dir = Path(args.chart_dir)
    if not chart_dir.is_dir():
        raise SystemExit(
            f"Chart not found at {chart_dir} - it lives in this repo (chart/); "
            f"pass --chart-dir to render a different one (CHART_PATH in the Makefile)"
        )

    if args.env_values and not args.env:
        parser.error("--env-values requires --env <name>")
    env_values = {}
    if args.env_values:
        import env_config
        env_values = env_config.load_environment_config(args.env).get("values") or {}

    zk_sources = [u.strip() for arg in args.zk_circuits_source for u in arg.split(",") if u.strip()]

    render(args.target, chart_dir, env=args.env, android_apk_key_hashes=args.android_apk_key_hash,
           namespace=args.namespace, out_dir=Path(args.out_dir), secrets_dir=Path(args.secrets_dir),
           mongo_password=args.mongo_password, conformance=args.conformance,
           dc_api_enable=args.dc_api_enable, zk_circuits_sources=zk_sources,
           hostnames=dict(h.split("=", 1) for h in args.hostname),
           mini_oidc_url=args.mini_oidc_url,
           credential_registries=[u.strip() for u in args.credential_registries.split(",") if u.strip()],
           env_values=env_values, resources=Resources(SIROSID_DEV_ROOT))


if __name__ == "__main__":
    main()
