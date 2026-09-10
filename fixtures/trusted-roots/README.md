# Trusted roots for interop testing

PEM-encoded CA certificates passed to `make fly-up`'s `TRUSTED_VERIFIER_ROOTS=`
flag (go-trust's `additional_trusted_roots`, go-trust#123+) - for a verifier
whose request-signing certificate is issued by a long-lived, self-signed
"reader CA" root meant to be trusted out-of-band per ISO 18013-5 convention,
rather than a public CA. See `pkg/registry/static/whitelist.go`'s
`AdditionalTrustedRoots` doc comment in go-trust for the full rationale.

## multipaz-reader-ca.pem

Fetched from `https://verifier.multipaz.org/verifier/readerRootCert` on
2026-08-13. Subject/issuer: `verifier.multipaz.org Reader CA`, self-signed,
valid 2025-06-19 to 2030-06-19. Confirmed distinct from
`verifier.multipaz.org`'s ordinary Let's Encrypt-issued HTTPS/TLS
certificate - this root only signs its OpenID4VP request-signing leaf
certificate (the `x509_san_dns` scheme's chain-validation target), never
appears in any OS system CA pool, and so needs to be trusted explicitly
here rather than relying on `x509_hash`-pinning a leaf that would break on
the verifier's next signing-key rotation.

Usage: `make fly-up ENV=<name> TRUSTED_VERIFIER_ROOTS=fixtures/trusted-roots/multipaz-reader-ca.pem TRUSTED_VERIFIERS=https://verifier.multipaz.org ...`

## siros-multipaz-verifier-reader-ca.pem

Reader-CA root for **our own** deployed multipaz-ppid verifier fork
(`https://siros-multipaz-verifier.fly.dev`, task #280) - distinct from
`multipaz-reader-ca.pem` above, which is the third-party
`verifier.multipaz.org`'s root. Captured 2026-08-16 at deploy time. Subject/
issuer: `Verifier Root at https://siros-multipaz-verifier.fly.dev/records`,
self-signed, valid 2026-08-16 to 2041-08-12. Notably has a **negative serial
number** (`openssl x509 -noout -serial` prints `(Negative)36:f4:...`) - this
is exactly the case go-trust 0.15.0's negative-serial CA cert support was
added for; a PDP pinned below that version will reject this root outright
regardless of `AdditionalTrustedRoots` wiring.

Usage: `make fly-up ENV=<name> TRUSTED_VERIFIER_ROOTS=fixtures/trusted-roots/siros-multipaz-verifier-reader-ca.pem TRUSTED_VERIFIERS=https://siros-multipaz-verifier.fly.dev ...`

## geneva2026-rical-root.pem

Not a `TRUSTED_VERIFIER_ROOTS` entry - this signs a **RICAL** (ISO 18013-5
2nd ed. Annex F reader-trust list), a different trust mechanism from the
`additional_trusted_roots` PEM-pinning above. Subject/issuer: `Trusted Lists
Root CA Certificate Geneva 2026`, self-signed, `C=CH, O=Aptitude`, valid
2026-06-11 to 2046-06-11 (sha256 fingerprint
`04:F8:A0:27:FC:25:4E:51:62:C4:3F:11:5A:AF:4A:31:B2:D6:CD:23:AF:74:26:B5:AC:EE:A6:7B:01:C1:89:BF`).
Extracted 2026-08-30 from the `geneva2026/` folder of certs the event
organizers distributed. Confirmed (decoding the COSE_Sign1 envelope of the
event's live `Rical.rical` document) that this root is the actual issuer of
the embedded `RICAL Signer Certificate` - not just a same-name guess.

The RICAL document itself is fetched live from
`https://geneva2026.mdoc.online/TrustedLists/Rical.rical` (the `latestRicalUrl`
field inside that same document) - go-trust's `mdocrical` registry refetches
and re-verifies it against this root on its own cache TTL, so this repo only
needs to carry the root, never the RICAL document itself.

Usage: `make fly-up ENV=<name> RICAL_PROVIDER_URL=https://geneva2026.mdoc.online/TrustedLists/Rical.rical RICAL_ROOT_CERT=fixtures/trusted-roots/geneva2026-rical-root.pem` (already persisted in `environments/gdc.yaml`, so a plain `make fly-up ENV=gdc` picks it up automatically).

## geneva2026-verifier-reader-ca.pem

This root is the one that lets a wallet trust remote OpenID4VP presentation
requests (`client_id_scheme=x509_san_dns`,
`client_id=x509_san_dns:geneva2026.mdoc.online`) from the event's reference
verifier - `geneva2026-rical-root.pem` above only covers RICAL (ISO 18013-5
BLE/NFC proximity mdoc-reader-auth), a separate trust check from the
`credential-verifier` AuthZEN action a remote OpenID4VP request goes
through. This cert's embedded subjectAltName URI of
`https://geneva2026.mdoc.online` confirms it's the right root for that
verifier identity, not a same-name guess. Subject/issuer: `Reader CA
Certificate Default Relying Party Geneva 2026, C=CH, O=Aptitude`,
self-signed, valid 2026-06-11 to 2046-06-11 (sha256 fingerprint
`FE:9E:2A:ED:30:87:D2:0C:26:E1:2E:53:63:FC:EB:93:30:24:E6:B0:2F:82:C9:BE:8F:27:0D:20:C1:6C:3D:CA`).
Extracted from the same `geneva2026/` folder of event-organizer-distributed
certs as `geneva2026-rical-root.pem` (`Reader CA Certificate Default Relying
Party Geneva 2026.cer` - there's also a second cert in that folder, `Reader
CA Certificate Relying Party not on RICAL Geneva 2026.cer`, deliberately
unused here since it's the event's negative-test-case root, not this
verifier's).

**Needs go-trust >= 0.20.5.** This cert's key uses the `brainpoolP256r1`
curve (`openssl x509 -noout -text` -> `ASN1 OID: brainpoolP256r1`), which
Go's standard `crypto/x509` package does not support natively - unlike NIST
P-256/P-384/P-521. Before go-trust#153 (fixed in 0.20.5), the whitelist
registry parsed `additional_trusted_roots` PEMs directly via stdlib
`x509.CertPool.AppendCertsFromPEM`, which doesn't just fail to add *this*
root on an unsupported curve - it fails the *entire* CA pool construction
for the whole registry ("system CA pool unavailable: additional_trusted_
roots[N]: failed to parse PEM certificate"), denying every other whitelisted
verifier too. Confirmed live 2026-08-31: adding this root without the fix
briefly broke trust for `verifier.multipaz.org` and
`siros-multipaz-verifier.fly.dev` as a side effect. go-trust#153 wires a
`CryptoExt` (the same brainpool-aware parsing RICAL/VICAL already had, per
go-trust's 0.18.0 changelog) into the whitelist registry's
`additional_trusted_roots` path too - `values-fly.yaml`'s `images.pdp` pin
must be >= `0.20.5` for this entry to work.

Usage: `make fly-up ENV=<name> TRUSTED_VERIFIER_ROOTS=fixtures/trusted-roots/geneva2026-verifier-reader-ca.pem TRUSTED_VERIFIERS=https://geneva2026.mdoc.online ...` (persisted in `environments/gdc.yaml`, so a plain `make fly-up ENV=gdc` picks it up automatically).
