#!/usr/bin/env python3
"""Patch fixtures/vc-config.yaml for a named Fly.io environment.

Same idea as scripts/generate-tunnel-config.py (patch the shared base config
rather than hand-maintain a second one), but retargeting every public-facing
URL at the environment's Fly hostnames instead of a Cloudflare tunnel.

The base config hardcodes a `vc-proxy:8443`/`vc-proxy:8445`-style reverse
proxy (only present in docker-compose.conformance.yml) as apigw's and
registry's public identity respectively - on Fly each of apigw/registry gets
its own dedicated app + hostname instead, so those two placeholder values
(though textually identical in the base file) are patched to two different
real hostnames, not just one.

apigw.auth_providers.oidc (the PID/EHIC OIDC-backed issuance flow) is
repointed at the environment's own mini-oidc Fly app instead of the
Android-emulator-only bridge address (192.168.240.1) the base config
hardcodes - see scripts/fly_common.py's mini_oidc_config(). Without this,
those four credential types can't be issued by ANY client, web or native,
once actually deployed (the emulator bridge address is unreachable from a
real network regardless of client type).

Native app support (Android/iOS, see the Fly-up module docstring for the
assetlinks.json/apple-app-site-association side of this):
- OpenID4VP presentation requests already work for native apps with zero
  config changes - the verifier hardcodes the `openid4vp://cb` deep link
  scheme unconditionally (internal/verifier/apiv1/handler_oidc.go in the vc
  repo), it's not behind any config flag.
- OpenID4VCI credential offers are different: the URI scheme used to hand a
  wallet an offer is *not* hardcoded - it's whatever `delivery.openid4vci.
  credential_offers.wallets.<id>.redirect_uri` says (vc repo's
  internal/apigw/apiv1/handlers_vctm.go), used verbatim, so a `native` wallet
  entry using the standard `openid-credential-offer://` scheme is added
  here alongside the existing `local` (web) one.

Unlike generate-tunnel-config.py's regex patches (which preserve the base
file's comments for a file developers may read directly), this does a full
parse/mutate/dump - the output is a new, gitignored, per-environment file
(fixtures/rendered/fly-<env>/vc-config.yaml), not something meant to be
hand-edited.
"""
import argparse
from pathlib import Path

import yaml

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def fly_url(env: str, component: str) -> str:
    return f"https://sirosid-{env}-{component}.fly.dev"


def fly_internal(env: str, component: str, port: int) -> str:
    return f"sirosid-{env}-{component}.internal:{port}"


def patch(config: dict, env: str) -> dict:
    apigw_url = fly_url(env, "vc-apigw")
    registry_url = fly_url(env, "vc-registry")
    verifier_url = fly_url(env, "vc-verifier")
    frontend_url = fly_url(env, "wallet-frontend")

    config["common"]["mongo"]["uri"] = f"mongodb://{fly_internal(env, 'mongodb', 27017)}"

    apigw = config["apigw"]
    if frontend_url not in apigw["api_server"]["cors"]["allowed_origins"]:
        apigw["api_server"]["cors"]["allowed_origins"].append(frontend_url)
    apigw["public_url"] = apigw_url
    apigw["registry_public_url"] = registry_url
    apigw["issuer_client"]["addr"] = fly_internal(env, "vc-issuer", 8090)
    apigw["registry_client"]["addr"] = fly_internal(env, "vc-registry", 8090)
    apigw["delivery"]["openid4vci"]["token_endpoint"] = f"{apigw_url}/token"
    apigw["delivery"]["openid4vci"]["clients"]["e2e-test-client-2"]["redirect_uri"] = f"{frontend_url}/cb"
    apigw["delivery"]["credential_offers"]["issuer_url"] = apigw_url
    apigw["delivery"]["credential_offers"]["wallets"]["local"]["redirect_uri"] = f"{frontend_url}/cb"
    # Standard OpenID4VCI same-device scheme (registered by both native
    # sample apps - AndroidManifest.xml/Info.plist) so a native wallet can be
    # handed an offer directly via the /offers/:scope/:wallet_id chooser,
    # not just the web wallet.
    apigw["delivery"]["credential_offers"]["wallets"]["native"] = {
        "label": "Native app (OpenID4VCI same-device)",
        "redirect_uri": "openid-credential-offer://",
    }

    # PID/EHIC OIDC-backed issuance - see module docstring. mini-oidc's own
    # env vars (fly-up.py) set APIGW_REDIRECT_URI to this same value.
    apigw["auth_providers"]["oidc"]["issuer_url"] = fly_url(env, "mini-oidc")
    apigw["auth_providers"]["oidc"]["redirect_uri"] = f"{apigw_url}/oidcrp/callback"

    issuer = config["issuer"]
    # issuer's public identity is deliberately the same as apigw's (matches
    # the base config's existing pattern - wallets never reach vc-issuer
    # directly, only via apigw).
    issuer["issuer_url"] = apigw_url
    issuer["jwt_attribute"]["issuer"] = apigw_url

    verifier = config["verifier"]
    verifier["public_url"] = verifier_url
    verifier["outbound"]["oidc_provider"]["issuer"] = verifier_url
    verifier["inbound"]["openid4vp"]["token_endpoint"] = f"{verifier_url}/token"
    verifier["inbound"]["openid4vp"]["clients"]["e2e-test-client"]["redirect_uri"] = f"{frontend_url}/cb"

    config["registry"]["public_url"] = registry_url
    # Default section_size (1M decoys, built as one in-memory slice before a
    # single bulk insert on first boot) OOM-killed vc-registry at Fly's
    # default 256MB machine size. A throwaway per-environment test/demo
    # deployment doesn't need production-scale decoy privacy sets - trimmed
    # here rather than only papering over it with more memory.
    config["registry"]["token_status_lists"]["section_size"] = 10000

    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True)
    parser.add_argument("--base", default=str(SIROSID_DEV_ROOT / "fixtures" / "vc-config.yaml"))
    parser.add_argument("--out", default=None,
                         help="default: fixtures/rendered/fly-<env>/vc-config.yaml")
    args = parser.parse_args()

    base_path = Path(args.base)
    config = yaml.safe_load(base_path.read_text())
    patched = patch(config, args.env)

    out_path = Path(args.out) if args.out else (
        SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}" / "vc-config.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(patched, sort_keys=False))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
