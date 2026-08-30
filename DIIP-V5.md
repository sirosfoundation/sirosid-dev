# DIIP v5 Compliance — SIROS ID Wallet

Conformance record for the [Decentralized Identity Interop Profile
v5](https://github.com/FIDEScommunity/DIIP/blob/main/spec/spec.md) (Release v5, approved by the
FIDES Community on 15 January 2026).

> **Read the spec from the repo, not the rendered site.** At the time of writing,
> <https://fidescommunity.github.io/DIIP/> still serves v4 (OID4VCI draft 15, OID4VP draft 28,
> SD-JWT VC draft 08) and `/draft/` is likewise stale. The authoritative v5 text is
> `spec/spec.md` on the `main` branch.

DIIP v5 chooses:

| Purpose | Specification |
| --- | --- |
| Credential format | W3C VCDM 2.0 and SD-JWT VC (draft 13) |
| Signature scheme | SD-JWT as specified in VC-JOSE-COSE (15 May 2025) |
| Signature algorithm | ES256 |
| Identifiers | did:jwk and did:web |
| Issuance protocol | OpenID for Verifiable Credential Issuance 1.0 (Final) |
| Presentation protocol | OpenID for Verifiable Presentations 1.0 (Final) |
| Revocation | IETF Token Status List (draft 15) |
| Trust establishment | OpenID Federation DCP — **OPTIONAL in v5** |

DIIP is a minimum: supporting more than it requires is compliant. The wallet keeps its mdoc
support, x509_san_dns verifier trust, and AuthZEN-based trust evaluation alongside the above.

## Requirement matrix

Holder-side requirements. ✅ = implemented, ➖ = optional in v5 and not implemented.

### Credential format and signatures

| Requirement | Status | Where |
| --- | --- | --- |
| MUST support SD-JWT VC | ✅ | `wallet-common/src/credential-parsers/SDJWTVCParser.ts`, `credential-verifiers/SDJWTVCVerifier.ts` |
| MUST support W3C VCDM secured with SD-JWT (VC-JOSE-COSE) | ✅ | `wallet-common/src/credential-parsers/VCDMSDJWTParser.ts`; verified by `SDJWTVCVerifier`, which resolves the VCDM `issuer` property as well as `iss` |
| MUST support ES256 | ✅ | key generation and signing throughout `wallet-frontend/src/services/keystore.ts` |

VC-JOSE-COSE §3.2.1 gives a VCDM credential the `typ` header `vc+sd-jwt` — the same string the
codebase already used for legacy SD-JWT VC. The two are distinguished by payload shape
(`vct` → SD-JWT VC; `@context` without `vct` → VCDM 2.0) in
`wallet-common/src/utils/detectCredentialFormat.ts`.

### Identifiers

| Requirement | Status | Where |
| --- | --- | --- |
| MUST support did:jwk and did:web for Issuers, Holders, Verifiers | ✅ | `wallet-common/src/resolvers/didResolver.ts`; holder keys in `wallet-frontend/src/services/keystore.ts` (`createDidJwk`) |

`DID_KEY_VERSION` selects how holder keys are identified. It defaults to `jwk` (did:jwk).
The earlier `p256-pub` and `jwk_jcs-pub` did:key variants still work, so wallets holding
credentials bound to a did:key keep functioning — no migration is required.

### Issuance (OID4VCI 1.0)

| Requirement | Status | Where |
| --- | --- | --- |
| Pre-Authorized Code Flow and Authorization Code Flow | ✅ | `wallet-frontend/src/lib/services/OpenID4VCI/OpenID4VCI.ts` |
| `tx_code` in the Pre-Authorized Code Flow | ✅ | `OpenID4VCI.ts`, `TxCodeInputContext.tsx` |
| MUST NOT assume the AS shares the Issuer's domain | ✅ | `OpenID4VCIHelper.getAuthorizationServerMetadata` resolves `oauth-authorization-server` separately |
| PKCE with `S256` | ✅ | `OpenID4VCI/OAuth/PushedAuthorizationRequest.ts` |
| PAR | ✅ | same — PAR is the only authorization request path |
| Issuer-initiated flow | ✅ | `handleCredentialOffer` |
| Same-device and cross-device Credential Offer | ✅ | URI handler and QR scanner both feed `handleCredentialOffer` |
| Immediate flow | ✅ | `credentialRequest` (deferred issuance is also supported) |
| `authorization_details` with `credential_configuration_id` | ✅ | `generateAuthorizationRequest` |
| `scope` parameter | ✅ | same |
| `jwt` proof type with did:jwk/did:web as `iss` and a `kid` from the DID document | ✅ | `keystore.generateOpenid4vciProofs` |
| `cnf` holder binding carrying a `kid` | ✅ | `keystore.resolveCnfKid`, consumed by `signJwtPresentation` and `deriveHolderKidFromCredential` |

Both credential-request parameters are sent: `authorization_details` whenever the Authorization
Server does not explicitly exclude it, and `scope` whenever the credential configuration declares
one.

### Presentation (OID4VP 1.0)

| Requirement | Status | Where |
| --- | --- | --- |
| Same-device and cross-device flows | ✅ | `wallet-frontend/src/lib/services/OpenID4VP/OpenID4VP.ts` |
| Authorization Request passed by reference | ✅ | `OpenID4VPServerAPI.handleRequestUri` |
| `get` value for `request_uri_method` | ✅ | same — `post` is rejected with `unsupported_request_uri_method` rather than silently downgraded |
| `did` Client Identifier Scheme | ✅ | `OpenID4VPServerAPI.parseClientIdScheme` accepts the OID4VP `decentralized_identifier:` prefix and a bare `did:` |

Features v5 explicitly does not require — presentations without holder binding, verifier
attestations, SIOPv2, encrypted responses, transaction data, the Digital Credentials API — are
untouched. The wallet implements several of them anyway.

### Validity and revocation

| Requirement | Status | Where |
| --- | --- | --- |
| MUST check `validFrom` / `validUntil` when specified | ✅ | `wallet-common/src/utils/credentialValidity.ts`, applied in `checkValidityAndStatus.ts` |
| MUST support IETF Token Status List | ✅ | `wallet-common/src/utils/tokenStatusList.ts` |

A status list that cannot be fetched produces a console warning, not a verification failure —
otherwise an offline wallet, or an issuer outage, would hide credentials the user legitimately
holds. Status results are cached for the token's `ttl`.

Credential status surfaces in the UI as `expired`, `notYetValid`, `revoked` or `suspended`
(`wallet-frontend/src/context/CredentialsContext.ts`, `components/Credentials/ExpiredRibbon.jsx`).

### Trust establishment

| Requirement | Status | Notes |
| --- | --- | --- |
| OpenID Federation DCP entity configurations | ➖ | **OPTIONAL in DIIP v5.** The wallet uses AuthZEN + `go-trust` as its PDP instead. |

This is the known gap for DIIP v6, where the Trust Establishment requirements become mandatory
for Issuer and Verifier Agents. It does not affect v5 compliance, and the requirements land on
Agents rather than Wallets in any case.

## Verifying locally

```bash
make up VC=yes
```

Then:

1. Accept a pre-authorized credential offer with a `tx_code`.
2. Run an authorization-code issuance and confirm the PAR body carries `authorization_details`
   with the `credential_configuration_id`.
3. Present to a verifier using a `decentralized_identifier:did:web:…` client_id and
   `request_uri_method=get`.
4. Flip an entry in `vc-registry`'s status list — admin GUI at
   <http://localhost:9004/admin/login> (`admin` / `e2e-admin-password`), or `GET
   http://localhost:9004/statuslists/0` to read the Status List Token itself — and confirm the
   credential shows as revoked after re-verification. Note that credentials carry
   `registry.public_url` (`https://vc-proxy:8445`) as their `status_list.uri`, which is only
   reachable under `CONFORMANCE=yes`; see the README for details.

Unit coverage lives beside the implementation in both repos (`yarn test`).
