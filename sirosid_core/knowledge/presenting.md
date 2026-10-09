---
title: "Presenting credentials - verifier, DCQL, DC API, proximity"
summary: "How to test presentation against the instance's own verifier, an external verifier or digital-credentials.dev, what DCQL and the presentation-request templates are, and what is documented for BLE/NFC."
digest: "Present to this instance's vc-verifier (UI presets, or templates via OIDC scopes). An external verifier (digital-credentials.dev, a partner) must be in trusted_verifiers - often both its https origin and its x509_hash:. \"No credentials match DCQL query\" = format/vct mismatch."
order: 80
tags: [presenting, verifier, openid4vp, dcql, dc-api, digital-credentials, proximity, ble, nfc]
---
## Against the instance's own verifier

Open the instance's **vc-verifier** URL. Two ways to ask for a presentation:
- **Presets** in the verifier's own UI (selectable presentation options).
- **Presentation-request templates**, reached only through a real OIDC request to the verifier
  (`/authorize?scope=...`): the verifier also acts as an OpenID Provider, and each template binds OIDC
  scopes to a DCQL query and a claim mapping. The platform ships these templates:
  `eudi_pid_basic`, `eudi_pid_basic_arf15`, `eudi_pid_full`, `eudi_pid_age_verification` (PID),
  `eudi_ehic_standard` (EHIC), `ebw_oid_owner`, `eucc_company_extract`, `eu_poa_representation`,
  `iban_ov_account` (EU Business Wallet) and `pid_mdoc_zk_vega` (Vega ZK mdoc).

The verifier then hands the request to a wallet: same-device through an "open in wallet" link (the web
wallet, or the SIROS native sample app via its `openid4vp://cb` scheme), cross-device via QR, or through
the browser's DC API when enabled. Your instance's own verifier is always trusted by your instance's
wallet (it is on the PDP's verifier list).

## DCQL in one paragraph

The verifier's request carries a DCQL query: which credential **format** (`dc+sd-jwt` or `mso_mdoc`),
which type (`vct_values` for SD-JWT, `doctype_value` for mdoc) and which claim paths. The wallet matches
it against what it holds. Every field must match what the issuer actually issued: a request for
`vc+sd-jwt` or for the generic `urn:eudi:pid:1` matches nothing here (the PID is `dc+sd-jwt` with
`urn:eudi:pid:arf-1.8:1`), and ARF 1.5 and 1.8 PIDs have different claim paths. The symptom is
"No credentials match DCQL query".

## Against an external verifier

The wallet asks its instance's PDP whether the verifier is trusted, before showing anything. Add the
verifier to `trusted_verifiers` in the form the PDP compares (see `trust`), plus its reader-CA root in
`trusted_verifier_roots` when its signing certificate chains to a self-signed root. Then start the
presentation from the verifier's side.

### digital-credentials.dev (Google's DC API test verifier)

A stricter, independent mdoc verifier useful for catching COSE/mdoc conformance issues. To present an
mDL to it from an instance:
1. issue an `mdl` (or `pid_mdoc`) to the wallet;
2. trust the site as a verifier with **both** entries, because unsigned requests are identified by the
   origin and signed ones by a certificate hash:
   - `https://digital-credentials.dev`
   - the `x509_hash:...` value quoted in the PDP's denial of a signed request (paste it verbatim);
   no reader-CA root is needed for an `x509_hash:` subject;
3. consider `dc_api_enable: "true"` (the `dc-api` template) when also testing your own verifier through
   the browser API.

Example (fill in the hash from the denial):

```json
{"schema_version": 1, "trusted_verifiers": ["https://digital-credentials.dev", "x509_hash:PASTE_HASH_FROM_DENIAL"]}
```

## The Digital Credentials API switch

`dc_api_enable` overrides the verifier's DC API support: `"true"` on, `"false"` off (to test the
fallback), `""` platform default. It only affects this instance's verifier; whether a browser or phone
offers a wallet through the API depends on the client platform, not on the instance.

## Proximity (BLE / NFC)

The SIROS native SDKs support ISO 18013-5 proximity presentation, and the developer smoke test presents
an mDL issued by an environment over BLE to `siros-verifier-cli` (`siros-verify read --mode
peripheral`), checking that the device signature is valid. Nothing about proximity is configured in a
saved config: the mdoc reader-trust list (RICAL) used for reader authentication is a developer-CLI
environment setting (`rical_provider_url`) and is not available on the hosted service. Treat other
proximity claims as unverified.

Sources: `fixtures/vc-presentation-requests/eudi_pid.yaml`, `fixtures/vc-presentation-requests/eudi_ehic.yaml`, `fixtures/vc-presentation-requests/eu_business_wallet.yaml`, `fixtures/vc-presentation-requests/pid_mdoc_zk.yaml`, `values-base.yaml`, `environments/gdc.yaml`, `sirosid_core/render.py`, `sirosid_core/spec.py`, `sirosid_core/policy.py`, `README.md`, `CLAUDE.md`, https://developers.siros.org/sirosid/verifiers/concepts
