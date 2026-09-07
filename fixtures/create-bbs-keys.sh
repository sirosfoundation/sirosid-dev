#!/usr/bin/env bash
# Generate the issuer's blind BBS key pair.
#
# Not part of create-pki.sh, and not because of tidiness: a BBS secret key
# is a BLS12-381 scalar consumed inside the signing algebra rather than an
# ECDSA key that signs a digest. openssl cannot generate one, and no
# mainstream PKCS#11 HSM implements the curve, so this is a software key by
# construction. zk-cred-bbs's `bbs-keygen` is the generator.
#
# Both halves land in vc-pki/, which is gitignored. An environment reads
# them from there at render time via bbs_public_key_file /
# bbs_secret_key_file (see environments/bbs.yaml and scripts/env_config.py),
# so nothing has to be pasted anywhere. The public half is echoed at the end
# only because a wallet or verifier has to be handed it out of band: nothing
# publishes a BBS issuer key yet.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$here/vc-pki"
crate="${ZK_CRED_BBS_DIR:-$here/../../zk-cred-bbs}"
key_info="${BBS_KEY_INFO:-sirosid-dev}"

if [[ ! -d "$crate" ]]; then
    echo "zk-cred-bbs checkout not found at $crate" >&2
    echo "Clone it beside sirosid-dev, or set ZK_CRED_BBS_DIR." >&2
    exit 1
fi

mkdir -p "$out"

# `cargo run` rather than build-then-invoke: the binary is not always at
# $crate/target/release, because CARGO_TARGET_DIR moves it and this script
# has no business guessing where.
#
# The `cli` feature gates the binary so the crate's iOS/Android cross builds
# do not compile a host-only tool; see zk-cred-bbs's own Cargo.toml.
echo "Building and running bbs-keygen from $crate ..."
cargo run --quiet --release --manifest-path "$crate/Cargo.toml" --features cli --bin bbs-keygen -- \
    --out-dir "$out" \
    --secret bbs_issuer.sk \
    --public bbs_issuer.pk \
    --key-info "$key_info" \
    "$@"

echo
echo "Wrote $out/bbs_issuer.sk (secret, 0600) and $out/bbs_issuer.pk (public)."
echo "An environment with bbs_public_key_file/bbs_secret_key_file pointing at"
echo "these (environments/bbs.yaml does) picks them up at render time - nothing"
echo "to paste. Hand the public key to a wallet or verifier that needs it:"
echo
echo "  $(tr -d '\n' < "$out/bbs_issuer.pk")"
