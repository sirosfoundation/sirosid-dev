---
title: "Trust - trusted_issuers, trusted_verifiers, reader-CA roots"
summary: "How to make an instance's trust service (PDP) accept a partner's issuer or verifier, which identity string actually matches, and why the PDP can boot slowly."
digest: "trusted_issuers: https credential_issuer URLs (a vc issuer's vc-apigw public URL, never an internal host). trusted_verifiers: x509_san_dns verifiers and DC API origins as https://<host>, x509_hash: verbatim, DIDs as decentralized_identifier:did:...; self-signed reader CAs also need trusted_verifier_roots. Unreachable issuers slow PDP boot by minutes."
order: 60
tags: [trust, pdp, go-trust, whitelist, trusted_issuers, trusted_verifiers, trusted_verifier_roots, x509, did]
---
## What the PDP has by default

Every instance's PDP (go-trust) gets a whitelist built at deploy time:
- **issuers**: this instance's own vc-apigw URL;
- **verifiers**: this instance's own vc-apigw and vc-verifier URLs;
- an **mdoc IACA registry** that validates mdoc issuers' IACA certificates, limited to the same issuer
  list;
- `trust_x509_via_system_ca` on, so certificate-identified verifiers are checked against the system CA
  pool plus any `trusted_verifier_roots`.

The saved config only ever **adds** entries. Trust lists are fixed at deploy time: changing them means a
reconfigure (`reconfigure_instance`, see `lifecycle`; the components restart) or a new instance.

## To trust a partner issuer

Add its **credential_issuer identifier** to `trusted_issuers`:

```json
{"schema_version": 1, "trusted_issuers": ["https://issuer.example.org"]}
```

- The value must be the string the issuer puts in its offers and in the credentials' `iss`: for a
  SUNET/vc issuer (including another SIROS ID Dev instance) that is its **vc-apigw public URL**
  (`https://vc-apigw-<id>.sirosid.dev`, or `https://sirosid-<env>-vc-apigw.fly.dev` for a CLI
  environment). An issuer core service is internal and never appears as an identity; trusting it never
  resolves and the PDP cannot fetch its keys.
- Only https URLs are accepted; a bare host or a non-public host is rejected by validation.
- Entries go into the issuer list only. They never make that party a trusted verifier.
- Trusting an issuer does not list it in the wallet's "Add credential" page; start issuance from the
  partner's own credential offer (link or QR).
- For an mdoc issuer the IACA registry uses the same list, so the same entry covers both checks.

## To trust a partner verifier

Add the identity the PDP derives from the verifier's request to `trusted_verifiers`. The PDP normalizes
some subjects **before** comparing, so the entry must be written in the normalized form:

| the verifier identifies itself as | write | why |
|---|---|---|
| `x509_san_dns:<host>` client_id (signed request) | `https://<host>` | normalized to `https://<host>`; the `x509_san_dns:` spelling passes validation but **never matches** |
| `x509_san_uri:<uri>` | `<uri>` (an https URL) | normalized to the URI |
| `x509_hash:<hash>` | `x509_hash:<hash>`, verbatim | compared un-normalized; copy it from the wallet's/PDP's "not trusted" message |
| an unsigned DC API request | the origin, `https://<host>` | the subject is the browser-verified origin |
| `decentralized_identifier:did:web:<host>` | `decentralized_identifier:did:web:<host>` | the PDP strips the prefix; a bare `did:web:` entry is rejected by this platform's validator |

A verifier that sends both signed and unsigned requests can need two entries (for example its origin
and its `x509_hash:`). A DID verifier also needs the PDP's did:web resolver, which is not enabled by
default; enabling it takes a `values` override (`pdp.extraRegistries.didweb`, raw_values capability) - see
the `values` example in `config-reference`. Stripping the `decentralized_identifier:` prefix arrived in
go-trust 0.20.7, the version the platform's `default` channel pins; with an older PDP image (via
`images`) the prefixed and bare forms do not match each other.

## Self-signed reader CAs: trusted_verifier_roots

Some verifiers (an ISO 18013-5 convention) sign their requests with a certificate issued by their own
self-signed "reader CA" root, not a public CA. For `x509_san_dns:` / `x509_san_uri:` verifiers the PDP
still validates the certificate chain, so whitelist membership alone fails. Add the root's PEM text to
`trusted_verifier_roots` **and** the verifier to `trusted_verifiers`. A root survives the verifier's leaf
key rotations, which an `x509_hash:` pin does not. `x509_hash:` verifiers skip chain validation (the
hash pin is the trust decision), so they need no root.

Get a root from the verifier's operator or its published reader-root URL; check its subject and validity
before trusting it. Older go-trust releases rejected some real roots (negative serial numbers before
0.15.0, brainpoolP256r1 keys before 0.20.5); the default pin is newer than both, but a custom `pdp`
image may not be.

## Slow PDP boot with trusted_issuers

go-trust fetches the JWKS of every whitelisted entity once, synchronously, **before** its HTTP listener
starts. An issuer without a JWKS endpoint (typically an mdoc-only issuer) or an unreachable one burns the
whole discovery timeout: several minutes. Meanwhile the PDP is not answering, so the instance stays
`creating` (or `start`/`reset` take longer) and early requests can see "Issuer not trusted". This is
expected and not fatal: issuer checks at offer acceptance are resolution-only and do not need the JWKS.
Wait; if it never finishes, remove the entry. Do not add an issuer "just in case".

## Diagnosing "not trusted"

1. Issuer refused on accepting an offer: is the offer's `credential_issuer` exactly in `trusted_issuers`
   (scheme, host, no trailing path difference)? Is it the apigw URL, not an internal service?
2. Verifier refused: read the denial - it quotes the exact subject (for example
   `subject=x509_hash:...`). Add that string (normalized per the table) and, for chained certificates,
   the root.
3. A whitelisted entry with a 404 on JWKS fetch is usually the wrong URL (an internal host), not a trust
   bug.

Sources: `sirosid_core/render.py`, `sirosid_core/policy.py`, `environments/gdc.yaml`, `fixtures/trusted-roots/README.md`, `CLAUDE.md`, `../go-trust/README.md`
