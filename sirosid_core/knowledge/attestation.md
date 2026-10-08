---
title: "Wallet attestation (WIA)"
summary: "What wallet_attestation turns on in an instance, which components it changes, and what to expect."
digest: "wallet_attestation=true makes the wallet backend issue a wallet instance attestation (WIA, IETF attestation-based client auth, iss = the wallet-proxy URL) and authenticate to the issuer with it; vc-apigw and vc-verifier accept it and the PDP trusts this instance's wallet provider. Newer, less validated path."
order: 110
tags: [attestation, wia, wallet-attestation, client-attestation, oauth]
---
## What changes

`wallet_attestation: true` (also in the `wallet-attestation` and `interop` templates) switches four
things at deploy time:

| component | change |
|---|---|
| wallet-backend | `wallet_provider.wia.enabled` on, mode `ietf` (iss + kid based attestation, draft-ietf-oauth-attestation-based-client-auth; no x5c), `iss` and the attestation's audience = the instance's **wallet-proxy** URL |
| wallet-frontend | `WIA_ENABLED=true`, so the wallet actually requests and attaches the attestation |
| vc-apigw and vc-verifier | wallet attestation accepted: a wallet can authenticate with its WIA instead of a pre-registered client id, and the trust decision goes to the PDP |
| pdp | a `wallet-providers` list containing this instance's wallet-proxy URL, mapped to the wallet_provider role |

Without it, WIA is explicitly off in the wallet backend and the wallet authenticates as the registered
public client.

## What to expect

- It is **newer and less validated** than the standard stack. When something does not line up, the
  reason is in the wallet-backend and vc-apigw logs; the console cannot read logs, so say so rather than
  guess.
- The PDP trusts only *this* instance's wallet provider. A different wallet (another instance, a partner
  wallet) presenting its own WIA is not trusted unless it is added, and the saved config has no typed key
  for extra wallet providers.
- The developer smoke test confirms the path in wallet-backend's log with lines like `using
  OAuth-Client-Attestation authentication (client-signed PoP)` and `Server-side issuer trust evaluation
  ... "trusted":true`.

## Wallet attestation vs key attestation

They are different things. Wallet attestation (this key) authenticates the wallet instance to the
issuer. Key attestation is a proof type in the credential request saying where the holder's key lives;
it is chosen per issuer and is not controlled by a saved-config key.

Sources: `sirosid_core/render.py`, `sirosid_core/deploy.py`, `sirosid_core/templates.py`, `sirosid_core/policy.py`, `CLAUDE.md`
