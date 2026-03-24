#!/bin/bash
# 
# Create PKI certificates and keys for E2E testing with VC services.
#
# This script generates:
# - Root CA certificate and key
# - EC P-256 signing keys with certificate chain (for SD-JWT signing)
#
# Usage:
#   ./create-pki.sh
#
# The generated keys are for testing only and should not be used in production.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKI_DIR="${SCRIPT_DIR}/vc-pki"

echo "Creating PKI directory: ${PKI_DIR}"
mkdir -p "${PKI_DIR}"

# Check if keys already exist
if [ -f "${PKI_DIR}/signing_ec_private.pem" ]; then
    echo "PKI files already exist. Remove ${PKI_DIR} to regenerate."
    exit 0
fi

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

echo "Generating EC P-256 signing key pair..."
# Generate EC private key in PKCS8 format
openssl ecparam -name prime256v1 -genkey -noout -out /tmp/signing_ec_raw.pem
openssl pkcs8 -topk8 -nocrypt -in /tmp/signing_ec_raw.pem -out "${PKI_DIR}/signing_ec_private.pem"
openssl ec -in "${PKI_DIR}/signing_ec_private.pem" -pubout -out "${PKI_DIR}/signing_ec_public.pem"
rm /tmp/signing_ec_raw.pem

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

# Clean up temp files
rm -f /tmp/signing_ec.csr /tmp/signing_ec.conf /tmp/signing_ec.ext /tmp/ca.conf

echo ""
echo "PKI files created in ${PKI_DIR}:"
ls -la "${PKI_DIR}"
echo ""
echo "Done! You can now use 'make up-vc' to start VC services."
