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

Usage: `make fly-up ENV=<name> TRUSTED_VERIFIER_ROOTS=fixtures/trusted-roots/multipaz-reader-ca.pem ...`
