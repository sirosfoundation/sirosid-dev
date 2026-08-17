#!/usr/bin/env bash
#
# Create PKI certificates and keys for E2E testing with VC services.
#
# This script generates:
# - Root CA certificate and key
# - EC P-256 signing keys with certificate chain (for SD-JWT signing)
# - EC P-256 wallet-provider key attestation signing key + cert chain
#
# Usage:
#   ./create-pki.sh
#   PKI_DIR_OVERRIDE=/path/to/env-pki ./create-pki.sh   # e.g. per Fly environment
#
# Each artifact group is independently guarded by its own file-existence
# check, so re-running after adding a new artifact group only generates what's
# missing - existing keys already relied upon elsewhere (hardcoded thumbprints,
# mounted volumes) are never regenerated/invalidated.
#
# The generated keys are for testing only and should not be used in production.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKI_DIR="${PKI_DIR_OVERRIDE:-${SCRIPT_DIR}/vc-pki}"

echo "Creating PKI directory: ${PKI_DIR}"
mkdir -p "${PKI_DIR}"

if [ -f "${PKI_DIR}/rootCA.key" ]; then
    echo "Root CA already exists, skipping."
else
    echo "Generating Root CA..."
    cat > /tmp/ca.conf <<EOF
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn

[dn]
C  = SE
ST = Test
L  = E2E Testing
O  = Wallet E2E Test
OU = Test PKI
CN = E2E Test Root CA
EOF

    openssl genrsa -out "${PKI_DIR}/rootCA.key" 2048
    openssl req -x509 -new -nodes -key "${PKI_DIR}/rootCA.key" -sha256 -days 3650 -out "${PKI_DIR}/rootCA.crt" -config /tmp/ca.conf
    rm -f /tmp/ca.conf
fi

if [ -f "${PKI_DIR}/signing_ec_private.pem" ]; then
    echo "EC signing key pair already exists, skipping."
else
    echo "Generating EC P-256 signing key pair..."
    # Generate EC private key in PKCS8 format
    openssl ecparam -name prime256v1 -genkey -noout -out /tmp/signing_ec_raw.pem
    openssl pkcs8 -topk8 -nocrypt -in /tmp/signing_ec_raw.pem -out "${PKI_DIR}/signing_ec_private.pem"
    openssl ec -in "${PKI_DIR}/signing_ec_private.pem" -pubout -out "${PKI_DIR}/signing_ec_public.pem"
    rm -f /tmp/signing_ec_raw.pem

    # Create CSR config for signing certificate
    cat > /tmp/signing_ec.conf <<EOF
[req]
default_bits       = 256
prompt             = no
default_md         = sha256
distinguished_name = dn

[dn]
C  = SE
ST = Test
L  = E2E Testing
O  = Wallet E2E Test
OU = Credential Signing
CN = E2E Test Credential Signer
EOF

    # Create extension file for signing certificate
    cat > /tmp/signing_ec.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = vc-issuer
DNS.3 = vc-verifier
EOF

    # Generate CSR and sign with rootCA
    openssl req -new -key "${PKI_DIR}/signing_ec_private.pem" -out /tmp/signing_ec.csr -config /tmp/signing_ec.conf
    openssl x509 -req -in /tmp/signing_ec.csr -CA "${PKI_DIR}/rootCA.crt" -CAkey "${PKI_DIR}/rootCA.key" \
        -CAcreateserial -out "${PKI_DIR}/signing_ec.crt" -days 730 -sha256 -extfile /tmp/signing_ec.ext

    # Create certificate chain PEM (cert + CA)
    cat "${PKI_DIR}/signing_ec.crt" "${PKI_DIR}/rootCA.crt" > "${PKI_DIR}/signing_ec_chain.pem"

    # Clean up signing temp files
    rm -f /tmp/signing_ec.csr /tmp/signing_ec.conf /tmp/signing_ec.ext
fi

if [ -f "${PKI_DIR}/wallet_provider_ec_private.pem" ]; then
    echo "EC wallet-provider key pair already exists, skipping."
else
    echo "Generating EC P-256 wallet-provider key attestation signing key..."
    # Generate EC private key in the "EC PRIVATE KEY" (SEC1) form that
    # go-wallet-backend's WalletProviderService.loadKeys expects (it also
    # accepts PKCS8 "PRIVATE KEY", but SEC1 matches this openssl default).
    openssl ecparam -name prime256v1 -genkey -noout -out "${PKI_DIR}/wallet_provider_ec_private.pem"
    # openssl defaults to 0600; match signing_ec_private.pem's world-readable
    # permissions so containers running as a different uid (e.g. the E2E
    # image's non-root user) can actually read the bind-mounted file - test-only
    # key material, no confidentiality need within the local fixtures dir.
    chmod 644 "${PKI_DIR}/wallet_provider_ec_private.pem"

    cat > /tmp/wallet_provider_ec.conf <<EOF
[req]
default_bits       = 256
prompt             = no
default_md         = sha256
distinguished_name = dn

[dn]
C  = SE
ST = Test
L  = E2E Testing
O  = Wallet E2E Test
OU = Wallet Provider Key Attestation
CN = E2E Test Wallet Provider
EOF

    cat > /tmp/wallet_provider_ec.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation
EOF

    openssl req -new -key "${PKI_DIR}/wallet_provider_ec_private.pem" -out /tmp/wallet_provider_ec.csr -config /tmp/wallet_provider_ec.conf
    openssl x509 -req -in /tmp/wallet_provider_ec.csr -CA "${PKI_DIR}/rootCA.crt" -CAkey "${PKI_DIR}/rootCA.key" \
        -CAcreateserial -out "${PKI_DIR}/wallet_provider_ec.crt" -days 730 -sha256 -extfile /tmp/wallet_provider_ec.ext

    rm -f /tmp/wallet_provider_ec.csr /tmp/wallet_provider_ec.conf /tmp/wallet_provider_ec.ext
fi

if [ -f "${PKI_DIR}/proxy_server.key" ]; then
    echo "TLS proxy server certificate already exists, skipping."
else
    echo "Generating TLS proxy server certificate..."
    # Server key for the vc-proxy (HTTPS reverse proxy for conformance testing)
    openssl ecparam -name prime256v1 -genkey -noout -out /tmp/proxy_raw.pem
    openssl pkcs8 -topk8 -nocrypt -in /tmp/proxy_raw.pem -out "${PKI_DIR}/proxy_server.key"
    rm -f /tmp/proxy_raw.pem

    cat > /tmp/proxy.conf <<EOF
[req]
default_bits       = 256
prompt             = no
default_md         = sha256
distinguished_name = dn

[dn]
C  = SE
ST = Test
L  = E2E Testing
O  = Wallet E2E Test
OU = VC Proxy
CN = vc-proxy
EOF

    cat > /tmp/proxy.ext <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = vc-proxy
DNS.2 = localhost
EOF

    openssl req -new -key "${PKI_DIR}/proxy_server.key" -out /tmp/proxy.csr -config /tmp/proxy.conf
    openssl x509 -req -in /tmp/proxy.csr -CA "${PKI_DIR}/rootCA.crt" -CAkey "${PKI_DIR}/rootCA.key" \
        -CAcreateserial -out "${PKI_DIR}/proxy_server.crt" -days 730 -sha256 -extfile /tmp/proxy.ext

    rm -f /tmp/proxy.csr /tmp/proxy.conf /tmp/proxy.ext
fi

echo ""
echo "PKI files in ${PKI_DIR}:"
ls -la "${PKI_DIR}"
echo ""
echo "Done! You can now use 'make up-vc' to start VC services."
