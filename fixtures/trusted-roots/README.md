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
