---
title: "Issuing credentials in an environment"
summary: "How a wallet gets a credential from an instance's issuer - the mini-oidc login, the synthetic users and their documents per scope, PID-authenticated types, and why \"no documents\" happens."
digest: "Issue from the web wallet (wallet-frontend URL, /id/default/): passkey sign-up, add a credential, pick a mini-oidc user (no password). Datastore types need a user who owns a document for that scope; EU Business Wallet types need a PID first. \"no documents\" = wrong user or stale datastore."
order: 70
tags: [issuing, openid4vci, mini-oidc, users, fixtures, pid, datastore, no-documents]
---
## The normal flow (wallet-initiated, authorization code)

1. Open the instance's **wallet-frontend** URL (the web wallet is under `/id/default/`; the root shows
   the environment dashboard with the URLs, RP ID and tenant).
2. Sign up with a passkey. The passkey belongs to this instance only (RP ID = the wallet-frontend host).
3. Add a credential: the wallet lists this instance's own issuer (vc-apigw), registered with the wallet
   backend at deploy and after every reset. Pick a credential type.
4. You are redirected to **mini-oidc**. Choose a test user from the list - there are no passwords.
5. Back in the wallet, the credential is stored.

For a native app, set its backend URL to the instance's **wallet-proxy** URL (see `mobile-testing`). The
issuer's OAuth client for wallets accepts the web wallet's callback and the SDK sample app's
`siros-sample://callback` deep link.

## The test users

mini-oidc's users: `alice-001`, `bob-002`, `carol-003` (natural persons) and `erik-010`, `maria-011`,
`jan-012`, `sophie-013` (people representing the fictional companies Nordic Tech Solutions AB, Grünberg
Consulting GmbH and Transport Dubois SARL). All names, addresses, companies and bank accounts are
fictional.

## Where the claims come from

Each type has an issuance source:
- **assertion**: built from mini-oidc's claims at issuance time; works for every user. Scopes
  `pid_1_5` and `ehic`.
- **datastore**: built from a document in `fixtures/vc-bootstrapping/<scope>.json`, keyed by the
  mini-oidc user id. The logged-in user must own a document for that scope.

Documents per scope (a `-full` document covers every optional claim too; the others only the
mandatory ones):

| scope | format | minimal documents | `-full` |
|---|---|---|---|
| `pid_1_8` | dc+sd-jwt | alice-001, bob-002, erik-010, maria-011, jan-012, sophie-013 | carol-003 |
| `mdl` | mso_mdoc | alice-001, bob-002 | carol-003 |
| `siros_id` | dc+sd-jwt | alice-001, bob-002 | carol-003 |
| `diploma` | dc+sd-jwt | alice-001, bob-002 | carol-003 |
| `pid_mdoc` | mso_mdoc | alice-001 | bob-002 |
| `mdl_zk4` | mso_mdoc | alice-001 | bob-002 |
| `photoid` | mso_mdoc | alice-001, bob-002 | carol-003 |
| `ebw_oid` | dc+sd-jwt | erik-010, maria-011 | sophie-013 |
| `eucc` | dc+sd-jwt | erik-010, maria-011, jan-012 | sophie-013 |
| `eu_poa` | dc+sd-jwt | jan-012 | sophie-013 |
| `iban_ov` | dc+sd-jwt | erik-010, maria-011, jan-012 | sophie-013 |

`mdl_zk4` is the 4-claim mDL for Vega ZK tests; its `-full` document (bob-002) fills all four circuit
slots. `siros_id` and `photoid` are defined, but are not in the scope list of the OAuth client the wallet
uses, so requesting them through the web wallet may be refused (unverified).

## PID-authenticated types (EU Business Wallet)

`ebw_oid`, `eucc`, `eu_poa` and `iban_ov` do not use mini-oidc. The issuer asks the wallet to
**present a PID** (OpenID4VP during issuance) and selects the person's document by matching the PID's
given name, family name and birth date against the identity mappings. So:
1. log in as a company user (erik-010, maria-011, jan-012 or sophie-013) and get a `pid_1_8` (or
   `pid_1_5`) first;
2. then request the attestation; present that PID when asked.

alice, bob and carol have no business documents, so their PID leads to "no documents".

## "no documents"

The issuer found no document for the identity. Check in this order:
1. Wrong user for the scope (see the table) - the most common cause.
2. For PID-authenticated types: the presented PID belongs to a user without a document, or its name /
   birth date does not match the identity mapping.
3. The datastore is stale: vc-apigw imports the fixture documents **only into an empty datastore**.
   An instance created before the fixtures changed does not get new documents on its own. Fix: `reset_instance`
   (wipes all data, re-imports everything) or create a new instance.

## Other issuance failures

- "Credential issuance failed" in the wallet is a sanitized message; the real reason is in vc-apigw's
  log (and wallet-backend's debug log). The console has no log access; say so instead of guessing.
- "Issuer not trusted" right after creation with `trusted_issuers` set: the PDP may still be booting
  (see `trust`).
- A wallet refusing a credential because its `vct` differs from what the offer advertised: use the
  `siros-registry` template (see `registries`).

## Partner issuers and cross-device offers

- To receive a credential from a partner's issuer, add its identifier to `trusted_issuers` and start from
  the partner's own credential offer (link or QR) in the wallet. It will not appear in the wallet's
  issuer list.
- The developer CLI has `make datastore-offer` to mint a pre-authorized offer for one document. It needs
  the environment's admin-API key held by whoever deployed it, so it is not available for hosted
  instances, and it is reported not to work against current vc main (unverified here). Do not offer it.

Sources: `README.md`, `CLAUDE.md`, `values-base.yaml`, `fixtures/vc-bootstrapping/identity_mappings.json`, `fixtures/vc-bootstrapping/pid_1_8.json`, `sirosid_core/render.py`, `sirosid_core/deploy.py`, `scripts/bootstrap.py`, `scripts/datastore.py`, `../mini-oidc/README.md`
