---
title: "Concepts - roles, formats, protocols, trust"
summary: "Wallet, issuer, verifier, trust service (PDP), registry and mini-oidc; SD-JWT VC vs mdoc; OpenID4VCI and OpenID4VP; what \"trusted\" means in an environment."
digest: "Wallet = wallet-frontend + wallet-backend (public via wallet-proxy). Issuer = vc-apigw (+ internal vc-issuer). Verifier = vc-verifier. Trust = the pdp (go-trust); it only decides trust, it does not list issuers in the wallet. Formats: dc+sd-jwt (by vct) and mso_mdoc (by doctype)."
order: 20
tags: [concepts, wallet, issuer, verifier, pdp, trust, registry, mdoc, sd-jwt, openid4vci, openid4vp]
---
## The roles

**Wallet.** The holder's side. In an environment it is two components: `wallet-frontend` (the web
wallet, a wwWallet-based single-page app) and `wallet-backend` (go-wallet-backend: user accounts,
passkey login, its own OAuth authorization server for the wallet's clients, credential storage and the
engine that runs issuance and presentation flows). `wallet-proxy` is the backend's public URL; native
apps (the SIROS SDK sample apps, the wallet wrappers) talk to it instead of the web frontend.

**Issuer.** SUNET/vc services. `vc-apigw` is the public issuer: it publishes OpenID4VCI metadata
(`/.well-known/openid-credential-issuer`), runs the token and credential endpoints, authenticates the
user (through mini-oidc, or by asking the wallet to present a PID) and looks up the claims. `vc-issuer`
is internal and only signs. `vc-registry` holds status lists and type metadata. The issuer identity that
wallets and the PDP see is always the **vc-apigw public URL**.

**Verifier.** `vc-verifier`: requests presentations with OpenID4VP (DCQL queries built from
presentation-request templates), optionally through the browser's Digital Credentials API.

**Trust service (PDP).** `pdp` is go-trust, an AuthZEN Policy Decision Point. The wallet backend asks
it "is this issuer trusted?" when it accepts a credential offer and "is this verifier trusted?" before
answering a presentation request; the vc services ask it about wallets when wallet attestation is on.
go-trust can combine several registries (whitelists, ETSI trust lists, OpenID Federation, did:web, mdoc
IACA lists); an environment uses a **whitelist** of its own vc-apigw and vc-verifier URLs plus whatever
the saved config adds, and an **mdoc IACA registry** for mdoc issuers.

**Registry (credential types).** Each credential type is described by a metadata document: a VCTM for
SD-JWT VC, an MDDL schema for mdoc. Both the issuer (to build credentials and DCQL queries) and the wallet
backend (to render and match them) need the same document. They come either from the platform's
vendored copies or from an external registry such as https://registry.siros.org (see `registries`).

**mini-oidc.** A test OpenID Provider that stands in for a national eID login during issuance. You pick
a user from a list; there are no passwords. It is the only "identity provider" in an environment.

## Credential formats

| format id | what | identified by | examples here |
|---|---|---|---|
| `dc+sd-jwt` | SD-JWT VC: a JWT with selectively disclosable claims | `vct` (a URN/URI) | `pid_1_8` (`urn:eudi:pid:arf-1.8:1`), `pid_1_5`, `ehic`, `diploma`, `siros_id`, `ebw_oid`, `eucc`, `eu_poa`, `iban_ov` |
| `mso_mdoc` | ISO/IEC 18013-5 mdoc (CBOR, COSE-signed) | `doctype` | `mdl` (`org.iso.18013.5.1.mDL`), `pid_mdoc` (`eu.europa.ec.eudi.pid.1`), `photoid`, `mdl_zk4` |

`mdl_zk4` is a 4-claim mDL variant used for Vega zero-knowledge proof tests. A credential *scope*
(`pid_1_8`, `mdl`, ...) is the OpenID4VCI scope and the key used throughout the configs.

## Protocols

- **OpenID4VCI** (issuance). The wallet fetches the issuer's metadata, gets authorized
  (authorization-code flow with a user login, or a pre-authorized code carried in a credential offer),
  gets a token, proves possession of a key and receives the credential.
- **OpenID4VP** (presentation). The verifier sends a signed request with a DCQL query; the wallet
  checks the verifier with the PDP, the user consents, and the wallet returns the selected claims.
- **W3C Digital Credentials API (DC API).** A browser API that lets a web verifier ask the platform's
  wallets for a credential without a QR code or redirect. The verifier's support is switched by
  `dc_api_enable`.
- **WebAuthn passkeys.** How users log in to the wallet (and to this console). The RP ID is the
  wallet-frontend host; native apps need their signing key listed (`android_apps`).

## What "trusted" means here

"Trusted" is only ever the PDP's answer for one entity in one role:
- An issuer is trusted when its identity (the `credential_issuer` URL from the offer) is in the PDP's
  issuer list. Your instance's own vc-apigw is always in it; `trusted_issuers` adds more.
- A verifier is trusted when the subject the PDP derives from its request (an https URL / origin, an
  `x509_hash:` value, or a DID) is in the verifier list, and - for certificate-based schemes - its
  certificate chains to a root the PDP accepts (`trusted_verifier_roots` adds roots). See `trust`.
- Trust is not registration. The wallet's own "Add credential" list shows only the issuers registered
  with wallet-backend (this instance's own vc-apigw, registered at deploy and after a reset). Trusting a
  partner issuer lets the wallet *accept its offers*; it does not add it to that list.
- None of this is real-world trust: every key in an instance is self-generated.

Sources: `README.md`, `CLAUDE.md`, `values-base.yaml`, `sirosid_core/components.py`, `sirosid_core/render.py`, `sirosid_core/deploy.py`, `scripts/bootstrap.py`, `fixtures/vc-presentation-requests/eudi_pid.yaml`, `../go-trust/README.md`, `../go-wallet-backend/README.md`, `../mini-oidc/README.md`, https://developers.siros.org/sirosid/trust/, https://developers.siros.org/sirosid/verifiers/concepts
