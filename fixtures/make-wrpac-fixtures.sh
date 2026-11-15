#!/usr/bin/env bash
#
# Mint the WRPAC/WRPRC deployment the wrpac environment drills with, and stage
# the verifier's share of it into fixtures/siros-verifier-wrpac/.
#
# The two parties (ETSI TS 119 475's intermediary model):
#
#   - the INTERMEDIARY is this environment's own siros-verifier app: it holds
#     a WRPAC (the X.509 access certificate per ETSI TS 119 411-8) whose key
#     signs every OpenID4VP request object, and presents a third party's
#     registration certificate on that party's behalf;
#   - the FRONTED RP (NTRSE-WRPAC-FRONTED) is that third party: its WRPRC
#     names the intermediary in `act` (V1.2.1 Table 10, GEN-5.2.4-09), and its
#     registrar dataset is armed alongside as the 472-2 s6.3.2.2
#     verifier_info members.
#
# The deployment is minted under fixtures/wrpac-fly/deployment with its
# --base-url pointing at the wrpac-registrar fly app, so every issued
# certificate references the URLs the environment actually serves. The
# registrar's key material is NOT committed (see .gitignore); re-running this
# script is REFUSED against an existing deployment, exactly as siros-wrpac-tool
# refuses - rotating the Access CA would silently invalidate the issued
# material already baked into the environment.
#
# Usage:
#   fixtures/make-wrpac-fixtures.sh
#   WRPAC_TOOL=/path/to/siros-wrpac-tool fixtures/make-wrpac-fixtures.sh
#
# Requires siros-wrpac-tool v0.2.1+ (the act claim, siros-wrpac-tool#24).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIX="${SCRIPT_DIR}/wrpac-fly"
REGISTRAR_HOST="sirosid-wrpac-registrar.fly.dev"

# The tool is a separate repo; prefer an explicit path, then one built beside
# this checkout, then whatever is on PATH.
WRPAC_TOOL="${WRPAC_TOOL:-}"
if [ -z "${WRPAC_TOOL}" ]; then
    for candidate in \
        "${SCRIPT_DIR}/../../siros-wrpac-tool/siros-wrpac-tool" \
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
siros-wrpac-tool not found (v0.2.1+ is required for the act claim, #24).

Build it from the sibling checkout:

    git clone git@github.com:sirosfoundation/siros-wrpac-tool.git ../siros-wrpac-tool
    make -C ../siros-wrpac-tool build

or point at it explicitly:

    WRPAC_TOOL=/path/to/siros-wrpac-tool $0
MSG
    exit 1
fi

if [ -d "${FIX}/deployment" ]; then
    cat >&2 <<MSG
${FIX}/deployment already exists: refusing to re-mint. The Access CA key under
it is what the environment's issued WRPACs chain to, and regenerating it
would invalidate everything already deployed (siros-wrpac-tool's init refuses
for the same reason). Tear the environment down and remove the directory only
if you mean to start over.
MSG
    exit 1
fi

mkdir -p "${FIX}"

"${WRPAC_TOOL}" init -d "${FIX}/deployment" \
    --base-url "https://${REGISTRAR_HOST}" \
    --country SE --organization "SIROS Foundation"

# The intermediary: this environment's siros-verifier. `issue` (not `apply`)
# because the deployment must hold the private key - it is what signs the
# verifier's request objects.
"${WRPAC_TOOL}" issue -d "${FIX}/deployment" \
    --identifier "NTRSE-WRPAC-INTERMEDIARY" \
    --name "WRPAC Drill Intermediary" \
    --organization "SIROS Test AB" \
    --country SE \
    --support-uri "https://sirosid-wrpac-siros-verifier.fly.dev/support" \
    --entitlement "https://uri.etsi.org/19475/Entitlement/Service_Provider" \
    --out "${FIX}/intermediary"

# The fronted RP: a CSR-based spec whose presents_through names the
# intermediary - the reconciled WRPRC carries act.sub = the intermediary.
mkdir -p "${FIX}/specs"
openssl ecparam -name prime256v1 -genkey -noout -out "${FIX}/specs/fronted-rp.key"
openssl req -new -key "${FIX}/specs/fronted-rp.key" \
    -subj "/O=Fronted RP Test AB/C=SE" -out "${FIX}/specs/fronted-rp.csr"
cat > "${FIX}/specs/fronted-rp.yaml" <<'SPEC'
name: WRPAC Drill Fronted RP
organization: Fronted RP Test AB
identifier: NTRSE-WRPAC-FRONTED
country: SE
support_uri: https://fronted-rp.example.test/support
privacy_policy_uri: https://fronted-rp.example.test/privacy
csr: fronted-rp.csr
presents_through: NTRSE-WRPAC-INTERMEDIARY
entitlements:
  - https://uri.etsi.org/19475/Entitlement/PID_Provider
provides:
  - vct: urn:eudi:pid:arf-1.8:1
SPEC

"${WRPAC_TOOL}" apply -d "${FIX}/deployment" --from "${FIX}/specs"

# Publish everything the registrar serves: the register, the CRL, the status
# list, and both list formats (go-trust reads either; this environment's PDP
# reads the TSL, whose per-service types the tool sets).
"${WRPAC_TOOL}" publish -d "${FIX}/deployment"
"${WRPAC_TOOL}" lote -d "${FIX}/deployment"
"${WRPAC_TOOL}" tsl -d "${FIX}/deployment"

# ---- stage the verifier's share -------------------------------------------
SV="${SCRIPT_DIR}/siros-verifier-wrpac"
mkdir -p "${SV}"
cp "${FIX}/specs/fronted-rp.issued/wrprc.jwt" "${SV}/wrprc.jwt"
# The request-signing pair: the intermediary's WRPAC leaf + the Access CA.
cat "${FIX}/intermediary/wrpac.pem" "${FIX}/deployment/ca.pem" > "${SV}/request_chain.pem"
cp "${FIX}/intermediary/wrpac.key" "${SV}/request_key.pem"

python3 - "${FIX}" "${SV}" <<'PY'
import json, sys
fix, sv = sys.argv[1], sys.argv[2]
# The registrar's B.2 dataset (ETSI TS 119 472-2 s6.3.2.2): the fronted RP's
# rendered data. The core's build_verifier_info validates the members.
dataset = {
    "identifier": [{"type": "ntr", "identifier": "NTRSE-WRPAC-FRONTED"}],
    "srvDescription": [{"lang": "sv", "content": "WRPAC-drillens frontade part"}],
    "registryURI": "https://sirosid-wrpac-registrar.fly.dev/register.json",
    "intendedUseIdentifier": "urn:siros:wrpac-drill:pid-verification",
    "purpose": [{"lang": "sv", "content": "Verifiera identitet"}],
    "policyURI": "https://fronted-rp.example.test/privacy",
}
open(f"{sv}/verifier-info.json", "w").write(json.dumps(dataset, indent=2) + "\n")
print(f"staged: wrprc.jwt, request_key.pem, request_chain.pem, verifier-info.json -> {sv}")
PY

echo
echo "Next: make fly-up ENV=wrpac"
