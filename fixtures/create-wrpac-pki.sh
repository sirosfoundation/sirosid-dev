#!/usr/bin/env bash
#
# Create the WRPAC/WRPRC trust anchors and client certificates for local
# testing, using siros-wrpac-tool.
#
# Under CIR (EU) 2025/848 both issuers and verifiers are registered
# wallet-relying parties, so each presents two documents: an access certificate
# (WRPAC) saying who it is, and a registration certificate (WRPRC) saying what
# it registered for. This produces both for the local vc-issuer and vc-verifier,
# and publishes the deployment's trust anchors as an ETSI TS 119 602 LoTE *and*
# an ETSI TS 119 612 TSL, because go-trust can be pointed at either.
#
# Usage:
#   ./create-wrpac-pki.sh
#   WRPAC_DIR_OVERRIDE=/path/to/deployment ./create-wrpac-pki.sh
#   WRPAC_TOOL=/path/to/siros-wrpac-tool ./create-wrpac-pki.sh
#
# Re-running is safe: `init` refuses to run twice (the CA key, CRL number and
# status list indices must persist - reusing a status index would transfer a
# previous holder's revocation to a new certificate), and `apply` reconciles
# the client specs against the register rather than reissuing blindly.
#
# The generated keys are for testing only.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WRPAC_DIR="${WRPAC_DIR_OVERRIDE:-${SCRIPT_DIR}/wrpac-pki}"
CLIENTS_DIR="${SCRIPT_DIR}/wrpac-clients"
BASE_URL="${WRPAC_BASE_URL:-http://wrpac-registrar:8080}"

# The tool is a separate repo; prefer an explicit path, then one built beside
# this checkout, then whatever is on PATH.
WRPAC_TOOL="${WRPAC_TOOL:-}"
if [ -z "${WRPAC_TOOL}" ]; then
    for candidate in \
        "${SCRIPT_DIR}/../../siros-wrpac-tool/bin/siros-wrpac-tool" \
        "$(command -v siros-wrpac-tool 2>/dev/null || true)"; do
        if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
            WRPAC_TOOL="${candidate}"
            break
        fi
    done
fi

if [ -z "${WRPAC_TOOL}" ] || [ ! -x "${WRPAC_TOOL}" ]; then
    cat >&2 <<MSG
siros-wrpac-tool not found.

Build it from the sibling checkout:

    git clone git@github.com:sirosfoundation/siros-wrpac-tool.git ../siros-wrpac-tool
    make -C ../siros-wrpac-tool build

or point at it explicitly:

    WRPAC_TOOL=/path/to/siros-wrpac-tool $0
MSG
    exit 1
fi

echo "Using $(basename "${WRPAC_TOOL}") from ${WRPAC_TOOL}"

if [ -f "${WRPAC_DIR}/register.json" ]; then
    echo "WRPAC deployment already exists at ${WRPAC_DIR}, skipping init."
else
    echo "Creating the Access CA and registration certificate provider..."
    "${WRPAC_TOOL}" init -d "${WRPAC_DIR}" --base-url "${BASE_URL}"
fi

# Each client keeps its own key: the CSR is what is committed, and the
# deployment certifies the public key without ever holding the private half.
# Generated here rather than committed because these are test keys for a local
# stack, and a committed private key is a committed private key.
for client in vc-issuer vc-verifier; do
    csr="${CLIENTS_DIR}/${client}.csr"
    key="${CLIENTS_DIR}/${client}.key"
    if [ -f "${csr}" ]; then
        continue
    fi
    echo "Generating a key and CSR for ${client}..."
    openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -keyout "${key}" -out "${csr}" \
        -subj "/CN=$(grep -m1 '^name:' "${CLIENTS_DIR}/${client}.yaml" | cut -d' ' -f2-)" \
        2>/dev/null
    # World-readable for the same reason create-pki.sh's keys are: containers
    # run as a different uid and bind-mount these.
    chmod a+r "${key}"
done

echo "Reconciling client specs against the register..."
"${WRPAC_TOOL}" apply -d "${WRPAC_DIR}" --from "${CLIENTS_DIR}"

# Publish the anchors in both formats. go-trust reads TS 119 602 through its
# `lote` registry and TS 119 612 through its `etsi` registry, and which one a
# given consumer speaks is not something this stack should have to decide.
echo "Publishing trust anchors as a LoTE (TS 119 602)..."
"${WRPAC_TOOL}" lote -d "${WRPAC_DIR}" --distribution-point "${BASE_URL}/lote.json"

echo "Publishing trust anchors as a TSL (TS 119 612)..."
"${WRPAC_TOOL}" tsl -d "${WRPAC_DIR}" --distribution-point "${BASE_URL}/tsl.xml"

# The published lists are public documents by definition - a LoTE, a TSL, a CRL
# and a status list all exist to be fetched by anyone. The tool writes them
# 0640/0750 for the deployment's own sake, which leaves a container running as
# a different uid (nginx here) unable to read the bind mount, so widen just the
# published directory. The CA key beside it stays as it is.
chmod a+rx "${WRPAC_DIR}/public"
chmod a+r "${WRPAC_DIR}"/public/*

echo
echo "WRPAC deployment ready:"
echo "  deployment      ${WRPAC_DIR}"
echo "  published lists ${WRPAC_DIR}/public"
echo "  client material ${CLIENTS_DIR}/<id>.issued/"
echo
echo "Start the registrar and a go-trust reading both lists with:"
echo "  docker compose -f docker-compose.test.yml -f docker-compose.wrpac.yml up -d"
