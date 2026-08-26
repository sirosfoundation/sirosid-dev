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
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fly_common import MINI_OIDC_APIGW_CLIENT_ID, MINI_OIDC_APIGW_CLIENT_SECRET  # noqa: E402

SIROSID_DEV_ROOT = Path(__file__).resolve().parent.parent


def fly_url(env: str, component: str) -> str:
    return f"https://sirosid-{env}-{component}.fly.dev"


def fly_internal(env: str, component: str, port: int) -> str:
    return f"sirosid-{env}-{component}.internal:{port}"


def patch(config: dict, env: str, mongo_password: str = None, wallet_attestation: bool = False,
          zk_circuits_sources: list = None) -> dict:
    apigw_url = fly_url(env, "vc-apigw")
    registry_url = fly_url(env, "vc-registry")
    verifier_url = fly_url(env, "vc-verifier")
    frontend_url = fly_url(env, "wallet-frontend")
    # wallet-frontend runs with BASE_PATH=/id/default/ (fly-up.py's
    # _wallet_frontend_env(), same hardcoded "default" tenant
    # register_vc_services() uses) - its SPA router has no route for a bare
    # /cb outside that base path, so redirect_uris must include it or the
    # OID4VCI/OID4VP callback lands on a blank page (nginx serves the SPA
    # shell fine at /cb, status 200 - the React router just never mounts
    # anything for a path it doesn't recognize).
    frontend_cb_url = f"{frontend_url}/id/default/cb"

    # Authenticated - see render-helm-config.py's patch_wallet_backend_fly
    # for why (mongodb has no auth otherwise, reachable by any app in the
    # shared sirosfoundation org over Fly's 6PN network).
    if not mongo_password:
        print(
            f"WARNING: --mongo-password not set - rendering an UNAUTHENTICATED "
            f"mongodb URI for env '{env}'. Only safe within the same fly-up.py "
            f"invocation that set the matching Fly secret; for a one-off render, "
            f"just re-run 'make fly-up ENV={env}' instead.",
            file=sys.stderr,
        )
    mongo_auth = f"root:{mongo_password}@" if mongo_password else ""
    config["common"]["mongo"]["uri"] = f"mongodb://{mongo_auth}{fly_internal(env, 'mongodb', 27017)}/?authSource=admin"

    apigw = config["apigw"]
    if frontend_url not in apigw["api_server"]["cors"]["allowed_origins"]:
        apigw["api_server"]["cors"]["allowed_origins"].append(frontend_url)
    apigw["public_url"] = apigw_url
    apigw["registry_public_url"] = registry_url
    apigw["issuer_client"]["addr"] = fly_internal(env, "vc-issuer", 8090)
    apigw["registry_client"]["addr"] = fly_internal(env, "vc-registry", 8090)
    apigw["delivery"]["openid4vci"]["token_endpoint"] = f"{apigw_url}/token"
    # fly-up.py's register_vc_services() registers "e2e-test-client" (not
    # "e2e-test-client-2") as THE client_id go-wallet-backend's engine uses
    # for OID4VCI authorization_code flows against this tenant's issuer -
    # for every session, web and native alike (see that function's own
    # comment for why). Its redirect_uri is a base-config scalar
    # ("siros-sample://callback", the native app's deep link) - patching
    # e2e-test-client-2 instead (as this used to do) left the client
    # actually in use never registered with the web frontend's own
    # callback URL, so apigw's PAR endpoint rejected every web-initiated
    # authorization_code credential with invalid_client, and
    # go-wallet-backend's (unsafe, separately tracked) PAR-failure fallback
    # then produced a request vc-apigw's PAR-only /authorize can't accept
    # either - confirmed live via a diploma issuance attempt on gdc.
    # oauth2.RedirectURIs unmarshals either a scalar or a list, so appending
    # here (rather than overwriting) keeps the native scheme working too.
    existing_redirect_uris = apigw["delivery"]["openid4vci"]["clients"]["e2e-test-client"]["redirect_uri"]
    if isinstance(existing_redirect_uris, str):
        existing_redirect_uris = [existing_redirect_uris]
    if frontend_cb_url not in existing_redirect_uris:
        existing_redirect_uris.append(frontend_cb_url)
    apigw["delivery"]["openid4vci"]["clients"]["e2e-test-client"]["redirect_uri"] = existing_redirect_uris
    apigw["delivery"]["credential_offers"]["issuer_url"] = apigw_url
    apigw["delivery"]["credential_offers"]["wallets"]["local"]["redirect_uri"] = frontend_cb_url
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
    # Explicit, not relying on the base vc-config.yaml's client_id/secret
    # happening to match mini-oidc's own defaults - see
    # fly_common.MINI_OIDC_APIGW_CLIENT_ID/_SECRET (fly-up.py sets the same
    # constants as mini-oidc's APIGW_CLIENT_ID/_SECRET env vars).
    apigw["auth_providers"]["oidc"]["registration"]["preconfigured"]["client_id"] = MINI_OIDC_APIGW_CLIENT_ID
    apigw["auth_providers"]["oidc"]["registration"]["preconfigured"]["client_secret"] = MINI_OIDC_APIGW_CLIENT_SECRET

    # Opt-in: let wallets authenticate via OAuth-Client-Attestation (their WIA
    # alone) instead of a pre-registered client_id, delegating the trust
    # decision to the PDP - pairs with render-helm-config.py's
    # build_fly_values_overlay() wallet-providers whitelist entry and
    # patch_wallet_backend_fly's wia.issuer/omit_x5c. Leaves policy.rules
    # unset (default open: any PDP-trusted wallet is authorized), since this
    # is about proving the mechanism works, not restricting it yet.
    if wallet_attestation:
        apigw.setdefault("trust", {})
        apigw["trust"]["pdp_url"] = f"http://{fly_internal(env, 'pdp', 8080)}"
        apigw["trust"]["wallet_attestation"] = {"enabled": True}

    issuer = config["issuer"]
    # issuer's public identity is deliberately the same as apigw's (matches
    # the base config's existing pattern - wallets never reach vc-issuer
    # directly, only via apigw).
    issuer["issuer_url"] = apigw_url
    issuer["jwt_attribute"]["issuer"] = apigw_url
    # issuer calls vc-registry directly (not just apigw) to allocate a status
    # list entry when issuing a credential - the base config's docker-compose
    # service name ("vc-registry:8090") doesn't resolve on Fly's 6PN network,
    # which fails every real credential issuance with a gRPC "name resolver
    # error: produced zero addresses" (confirmed live).
    issuer["registry_client"]["addr"] = fly_internal(env, "vc-registry", 8090)

    verifier = config["verifier"]
    verifier["public_url"] = verifier_url
    verifier["outbound"]["oidc_provider"]["issuer"] = verifier_url
    verifier["inbound"]["openid4vp"]["token_endpoint"] = f"{verifier_url}/token"
    verifier["inbound"]["openid4vp"]["clients"]["e2e-test-client"]["redirect_uri"] = frontend_cb_url
    # Let a presentation be STARTED at the verifier and handed to the web
    # wallet by direct link, instead of only cross-device via QR. Two separate
    # registrations, because they cover different halves:
    #
    #   supported_wallets      puts an "open in <wallet>" link on the
    #                          verifier's presentation page. The verifier
    #                          appends client_id + request_uri to this base
    #                          URL, which is what wallet-frontend's
    #                          UriHandlerProvider consumes on its cb route.
    #
    #   oidc_provider.
    #     static_clients       is what /authorize actually validates against.
    #                          NOT inbound.openid4vp.clients above, despite
    #                          the name: getClientByID (vc's
    #                          internal/verifier/apiv1/client.go) checks the
    #                          datastore and then static_clients only, so a
    #                          client listed solely under
    #                          inbound.openid4vp.clients is rejected with
    #                          "invalid_client" - confirmed live locally.
    #
    # Public client (no secret): the wallet is a browser app and the redirect
    # target is its own static verification/result page, which never exchanges
    # the code. Mirrors the local path's equivalent in
    # scripts/generate-tunnel-config.py.
    verifier["supported_wallets"] = {"SIROS ID": frontend_cb_url}
    verifier["outbound"]["oidc_provider"]["static_clients"] = [
        {
            "client_id": "wallet-web",
            "token_endpoint_auth_method": "none",
            "redirect_uris": [f"{frontend_url}/id/default/verification/result"],
            "allowed_scopes": ["openid", "pid", "ehic", "diploma", "mdl"],
        }
    ]

    config["registry"]["public_url"] = registry_url
    # Default section_size (1M decoys, built as one in-memory slice before a
    # single bulk insert on first boot) OOM-killed vc-registry at Fly's
    # default 256MB machine size. A throwaway per-environment test/demo
    # deployment doesn't need production-scale decoy privacy sets - trimmed
    # here rather than only papering over it with more memory.
    config["registry"]["token_status_lists"]["section_size"] = 10000

    # verifier.zk_circuits.sources (pkg/model.ZkCircuitsConfig in vc) is
    # unset by default, so the deployed verifier falls back to vc's own
    # hardcoded https://zk-circuits.fly.dev - fine for the Longfellow
    # circuits already published there, but a circuit still gated
    # `published: false` on that real, public catalog (e.g. a Vega
    # variant awaiting its own expert review, or the r7-early-testing
    # circuit deployed on zk-circuits-test.fly.dev in the meantime - see
    # go-zk-circuits' publish/vega-mc-r7-early-testing PR) needs an
    # explicit, additional source ahead of the public default. Only set
    # when requested (fly-up.py's --zk-circuits-source) - most
    # environments need nothing here at all.
    if zk_circuits_sources:
        verifier.setdefault("zk_circuits", {})["sources"] = zk_circuits_sources

    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True)
    parser.add_argument("--base", default=str(SIROSID_DEV_ROOT / "fixtures" / "vc-config.yaml"))
    parser.add_argument("--mongo-password", default=None,
                         help="mongodb root password (see render-helm-config.py --mongo-password) - "
                              "fly-up.py passes the same value it set as the mongodb app's Fly secret.")
    parser.add_argument("--out", default=None,
                         help="default: fixtures/rendered/fly-<env>/vc-config.yaml")
    parser.add_argument("--wallet-attestation", action="store_true",
                         help="Enable apigw.trust.wallet_attestation so wallets can authenticate via "
                              "their WIA alone (no pre-registered client_id) - see render-helm-config.py's "
                              "--wallet-attestation, which must be passed alongside this for the wallet "
                              "side (wia.issuer/omit_x5c) to match.")
    parser.add_argument("--zk-circuits-source", action="append", default=None,
                         help="Additional verifier.zk_circuits.sources entry (repeatable), tried ahead "
                              "of vc's built-in https://zk-circuits.fly.dev default. Use for a circuit "
                              "not yet published there, e.g. https://zk-circuits-test.fly.dev while a "
                              "Vega circuit variant awaits its expert review.")
    args = parser.parse_args()

    base_path = Path(args.base)
    config = yaml.safe_load(base_path.read_text())
    patched = patch(config, args.env, args.mongo_password, args.wallet_attestation,
                     zk_circuits_sources=args.zk_circuits_source)

    out_path = Path(args.out) if args.out else (
        SIROSID_DEV_ROOT / "fixtures" / "rendered" / f"fly-{args.env}" / "vc-config.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump(patched, sort_keys=False))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
