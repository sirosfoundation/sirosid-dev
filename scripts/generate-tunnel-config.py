#!/usr/bin/env python3
"""Generate a tunnel-compatible vc-config from the base conformance config.

The base vc-config.yaml hardcodes "https://vc-proxy:8443" as the externally
reachable URL for apigw/issuer (credential_issuer, token_endpoint, JWT issuer
claim, etc.) — vc-proxy is a TLS-terminating reverse proxy that only exists in
the docker-compose.conformance.yml overlay. In the plain VC=yes stack (no
conformance overlay), vc-proxy doesn't exist at all, so any credential offer
built from this config embeds an unreachable credential_issuer — this fails
silently for a real device redeeming the offer over a Cloudflare tunnel, with
no error surfaced anywhere in facetec-api or vc-apigw (the offer generation
itself succeeds; only the wallet's later fetch to the bogus URL fails).

Patches:
  - Every occurrence of https://vc-proxy:8443 -> the vc-apigw tunnel URL
    (covers apigw.public_url, apigw.delivery.credential_offers.issuer_url,
    apigw.delivery.openid4vci.token_endpoint, issuer.issuer_url,
    issuer.jwt_attribute.issuer, and any future occurrence of the same host).
  - Adds the wallet-frontend tunnel URL to apigw.api_server.cors.allowed_origins,
    since the frontend (served from its own tunnel origin) calls vc-apigw
    (served from a different tunnel origin) cross-origin.
  - Registers an OAuth client in apigw.delivery.openid4vci.clients keyed by
    "{frontend_url}/" — go-wallet-backend's engine defaults the pre-authorized-
    code token request's client_id to its own configured OPENID4VCI_REDIRECT_URI
    (== "${TUNNEL_FRONTEND_URL}/") whenever the credential issuer isn't found in
    its own issuer registry, which is always true for facetec-api's siros_id
    issuer (it's dynamically tunneled, never registered). Without a matching
    client entry, vc-apigw's /token endpoint rejects the exchange with
    "invalid_client: Client authentication failed" — the offer looks fine and
    OpenIDFlowCallback even gets as far as trust evaluation before failing.
  - Appends "{frontend_url}/id/default/cb" to e2e-test-client's existing
    redirect_uri list — this is the DIFFERENT client the VC=yes apigw issuer
    (registered via `make register-vc-services`, always with client_id
    "e2e-test-client") actually uses for every authorization_code-flow scope
    (any auth_provider: oidc scope - pid/pid_1_5/pid_1_8/ehic/diploma), as
    opposed to the pre-authorized-code-only, dynamically-tunneled facetec
    siros_id issuer above. Both this fix and the client above are needed;
    they cover two different issuers with two different grant types. This
    URL is dynamic per tunnel session (a fresh *.trycloudflare.com host each
    time), so it can't be a static entry in the base vc-config.yaml the way
    the Fly equivalent (patch-vc-config-fly.py) can - it must be patched in
    here, freshly, every time a tunnel session starts.

vc-proxy:8445 (vc-registry) is intentionally left untouched — registry isn't
tunneled or reached directly by the FaceTec/siros_id flow this script exists
for (siros_id uses a local VCTM file, not a registry-hosted one).

The base vc-config.yaml stays untouched so conformance tests keep working.
"""
import argparse
import re
import sys
from pathlib import Path


def patch_config(
    base: str,
    apigw_url: str,
    frontend_url: str,
    oidc_issuer_url: str = "",
    oidc_redirect_uri: str = "",
    apigw_listen_port: str = "",
    verifier_url: str = "",
    verifier_listen_port: str = "",
    wallet_link_url: str = "",
    wallet_link_name: str = "Web wallet",
    wallet_oidc_redirect_uri: str = "",
    wallet_client_id: str = "wallet-web",
) -> str:
    out = base

    # 0a. The verifier has the same two-audiences problem as apigw, and the
    #     base config splits it the wrong way round: verifier.public_url (and
    #     the OIDC issuer / token_endpoint derived from it) is
    #     "http://localhost:9001", reachable from the host browser but not
    #     from inside the compose network, while register-vc-services hands
    #     wallet-backend "http://vc-verifier:8080", reachable from containers
    #     but not the browser. A presentation redirect then sends the user to
    #     a hostname their browser cannot resolve. One alias-backed URL, with
    #     the listen port aligned to the published one, satisfies both.
    if verifier_listen_port:
        out = re.sub(
            r'(\nverifier:\n(?:.*\n)*?\s*api_server:\n\s*addr:\s*)":8080"',
            rf'\g<1>":{verifier_listen_port}"',
            out,
            count=1,
        )
    if verifier_url:
        out = out.replace("http://localhost:9001", verifier_url)

    # 0b. Register the web wallet under verifier.supported_wallets, so the
    #     verifier's own presentation page offers a same-device "open in
    #     wallet" link instead of only a cross-device QR code. The verifier
    #     appends client_id + request_uri to this base URL, which is exactly
    #     what wallet-frontend's UriHandlerProvider consumes on the cb route.
    #
    #     Injected here rather than added to the base fixture because the base
    #     is shared: patch-vc-config-fly.py builds Fly's config from it and
    #     doesn't touch supported_wallets, so a hardcoded localhost:3000 entry
    #     would show up as a dead link in every Fly environment.
    if wallet_link_url:
        anchor = f'\n  public_url: "{verifier_url}"\n'
        if anchor in out:
            block = (
                f'{anchor}  supported_wallets:\n'
                f'    "{wallet_link_name}": "{wallet_link_url}"\n'
            )
            out = out.replace(anchor, block, 1)
        else:
            raise ValueError(
                "could not anchor supported_wallets on the verifier's public_url "
                "line - the base config's verifier section may have changed"
            )

    # 0c. Register the wallet as a static OIDC client of the verifier, so a
    #     presentation can be STARTED at the verifier (/authorize) rather than
    #     only from the wallet side.
    #
    #     Note this is a different map from the base config's
    #     verifier.inbound.openid4vp.clients, which despite the name is not
    #     what /authorize consults: getClientByID (vc's
    #     internal/verifier/apiv1/client.go) checks the datastore and then
    #     verifier.outbound.oidc_provider.static_clients, so a client listed
    #     only under inbound.openid4vp.clients is rejected with
    #     "invalid_client: Client authentication failed" - confirmed live.
    #
    #     Public client (token_endpoint_auth_method: none, hence no secret):
    #     the wallet is a browser app and the redirect target is a static
    #     confirmation page that doesn't exchange the code.
    if wallet_oidc_redirect_uri and verifier_url:
        anchor = f'\n      issuer: "{verifier_url}"\n'
        if anchor not in out:
            raise ValueError(
                "could not anchor static_clients on the verifier's oidc_provider "
                "issuer line - the base config's verifier section may have changed"
            )
        block = (
            f'{anchor}      static_clients:\n'
            f'        - client_id: "{wallet_client_id}"\n'
            f'          token_endpoint_auth_method: "none"\n'
            f'          redirect_uris:\n'
            f'            - "{wallet_oidc_redirect_uri}"\n'
            f'          allowed_scopes: ["openid", "pid", "ehic", "diploma", "mdl"]\n'
        )
        out = out.replace(anchor, block, 1)

    # 0. Repoint apigw's own listen port when asked. Only meaningful for the
    #    local path, where apigw's published port and its in-container port
    #    have to be the SAME number: the one URL it advertises must resolve to
    #    the container from inside the compose network (docker DNS -> container
    #    IP) and to loopback from the host browser (published port), and a URL
    #    carries exactly one port. apigw's is the first api_server block in the
    #    file (issuer/verifier/registry follow), hence count=1.
    if apigw_listen_port:
        out = re.sub(
            r'(\n\s*api_server:\n\s*addr:\s*)":8080"',
            rf'\g<1>":{apigw_listen_port}"',
            out,
            count=1,
        )

    # 1. Replace every occurrence of the unreachable vc-proxy:8443 URL.
    out = out.replace("https://vc-proxy:8443", apigw_url)

    # 1b. Repoint apigw.auth_providers.oidc (the PID/EHIC/diploma
    #     authorization_code flow) when asked. The base config hardcodes
    #     192.168.240.1 - the *Waydroid* gateway (docker-compose.android.yml's
    #     WAYDROID_GATEWAY default), not an address that exists on an ordinary
    #     dev machine - so apigw fails discovery against it and every
    #     oidc-backed scope dies with "OIDC RP not ready: OIDC RP not
    #     available". Mirrors scripts/patch-vc-config-fly.py's equivalent
    #     (oidc.issuer_url = mini-oidc's public URL, oidc.redirect_uri =
    #     apigw's own public URL + /oidcrp/callback).
    if oidc_issuer_url:
        out = re.sub(
            r'(\n\s*issuer_url:\s*)"http://192\.168\.240\.1:9005"',
            rf'\g<1>"{oidc_issuer_url}"',
            out,
            count=1,
        )
    if oidc_redirect_uri:
        out = re.sub(
            r'(\n\s*redirect_uri:\s*)"http://192\.168\.240\.1:8091/oidcrp/callback"',
            rf'\g<1>"{oidc_redirect_uri}"',
            out,
            count=1,
        )

    # 2. Add the frontend tunnel URL to apigw.api_server.cors.allowed_origins,
    #    right after the existing "http://localhost:3000" entry, if not already present.
    if frontend_url and f'"{frontend_url}"' not in out:
        out = re.sub(
            r'(allowed_origins:\s*\n\s*-\s*"http://localhost:3000"\s*\n)',
            rf'\1        - "{frontend_url}"\n',
            out,
            count=1,
        )

    # 3. Append this tunnel session's frontend callback URL to e2e-test-client's
    #    redirect_uri list (see module docstring point 4) - same insertion
    #    pattern as scripts/generate-android-config.py, which requires this
    #    exact "e2e-test-client":\n  type: ...\n  redirect_uri:\n  - ...
    #    structure (no comment lines directly inside the block). Deliberately
    #    done BEFORE step 4 below: that step's own redirect_uri literally
    #    equals this one ("{frontend_url}/id/default/cb"), so checking "not
    #    in out" after step 4 had already inserted it would false-positive
    #    and skip this append entirely.
    frontend_cb_url = f"{frontend_url}/id/default/cb"
    if frontend_url and f'"{frontend_cb_url}"' not in out:
        out = re.sub(
            r'("e2e-test-client":\s*\n\s*type:\s*"public"\s*\n\s*redirect_uri:\s*\n(?:\s*-\s*"[^"]*"\s*\n)*)',
            lambda m: m.group(0).rstrip("\n") + f'\n            - "{frontend_cb_url}"\n',
            out,
            count=1,
        )

    # 4. Register an OAuth client for go-wallet-backend's default (unregistered-issuer)
    #    client_id, so the pre-authorized-code token exchange isn't rejected with
    #    "invalid_client". Inserted right before credential_offers (a sibling of
    #    clients under delivery.openid4vci), matching the existing entries' indentation.
    client_id = f"{frontend_url}/"
    if frontend_url and f'"{client_id}"' not in out:
        new_client_block = (
            f'        "{client_id}":\n'
            f'          type: "public"\n'
            f'          redirect_uri: "{frontend_cb_url}"\n'
            f'          scopes:\n'
            f'            - "siros_id"\n'
        )
        marker = "\n    credential_offers:"
        if marker not in out:
            raise ValueError(
                "expected 'credential_offers:' under apigw.delivery.openid4vci "
                "in the base config — base config structure may have changed"
            )
        out = out.replace(marker, "\n" + new_client_block + "    credential_offers:", 1)

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
        default="fixtures/vc-config-tunnel.yaml",
        help="Output file (default: fixtures/vc-config-tunnel.yaml)",
    )
    parser.add_argument(
        "--apigw-url",
        required=True,
        help="Public tunnel URL for vc-apigw, e.g. https://xyz.trycloudflare.com",
    )
    parser.add_argument(
        "--frontend-url",
        required=True,
        help="Public tunnel URL for wallet-frontend, e.g. https://abc.trycloudflare.com",
    )
    parser.add_argument(
        "--oidc-issuer-url",
        default="",
        help="Repoint apigw.auth_providers.oidc.issuer_url (mini-oidc's reachable URL). "
        "Omit to leave the base config's value untouched.",
    )
    parser.add_argument(
        "--oidc-redirect-uri",
        default="",
        help="Repoint apigw.auth_providers.oidc.redirect_uri, normally "
        "<apigw-url>/oidcrp/callback. Omit to leave the base config's value untouched.",
    )
    parser.add_argument(
        "--apigw-listen-port",
        default="",
        help="Repoint apigw.api_server.addr to this port. Needed when apigw's "
        "published and in-container ports must match (the local path). Omit to "
        "leave the base config's :8080 untouched (tunnels, conformance, Fly).",
    )
    parser.add_argument(
        "--verifier-url",
        default="",
        help="Replace the verifier's http://localhost:9001 identity (public_url, "
        "OIDC issuer, token_endpoint). Omit to leave the base config untouched.",
    )
    parser.add_argument(
        "--verifier-listen-port",
        default="",
        help="Repoint verifier.api_server.addr to this port, for the same "
        "published/in-container alignment as --apigw-listen-port.",
    )
    parser.add_argument(
        "--wallet-link-url",
        default="",
        help="Register this wallet URL under verifier.supported_wallets, giving "
        "the verifier's presentation page a same-device 'open in wallet' link. "
        "Requires --verifier-url (used to anchor the insertion).",
    )
    parser.add_argument(
        "--wallet-link-name",
        default="Web wallet",
        help="Display name for --wallet-link-url on the verifier's page.",
    )
    parser.add_argument(
        "--wallet-oidc-redirect-uri",
        default="",
        help="Register a static OIDC client on the verifier with this redirect "
        "URI, so a presentation can be started at the verifier's /authorize. "
        "Requires --verifier-url (used to anchor the insertion).",
    )
    parser.add_argument(
        "--wallet-client-id",
        default="wallet-web",
        help="client_id for --wallet-oidc-redirect-uri.",
    )
    args = parser.parse_args()

    base_path = Path(args.base)
    if not base_path.exists():
        print(f"Error: base config not found: {base_path}", file=sys.stderr)
        sys.exit(1)

    apigw_url = args.apigw_url.rstrip("/")
    frontend_url = args.frontend_url.rstrip("/")

    base = base_path.read_text()
    patched = patch_config(
        base,
        apigw_url,
        frontend_url,
        args.oidc_issuer_url.rstrip("/"),
        args.oidc_redirect_uri,
        args.apigw_listen_port,
        args.verifier_url.rstrip("/"),
        args.verifier_listen_port,
        args.wallet_link_url.rstrip("/"),
        args.wallet_link_name,
        args.wallet_oidc_redirect_uri,
        args.wallet_client_id,
    )

    out_path = Path(args.output)
    out_path.write_text(patched)
    print(f"Generated {out_path} from {base_path}")
    print(f"  apigw/issuer URLs -> {apigw_url}")
    print(f"  CORS allowed_origins += {frontend_url}")


if __name__ == "__main__":
    main()
