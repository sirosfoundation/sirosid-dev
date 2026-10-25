# The vc verifier image plus the nym_auth circuit artifact, for environments that verify nym_auth pseudonym proofs
# (environments/gdc.yaml, verifier.nym_auth).
#
# The artifact is the DECOMPRESSED Longfellow circuit (1,062,842 bytes). It cannot travel as a Fly file secret
# (base64 of it exceeds the ~1 MiB machine config body) and the vc repo does not carry it, so it is baked in here.
# The size and digest are checked at build time against the values the allowlist entry in environments/gdc.yaml
# states, which are in turn the ones zk-cred-longfellow's src/nymauth/profile.rs ships; vc re-checks both at startup
# and refuses anything else, so a wrong file fails the build rather than the first presentation.
#
#   zstd -dc <archive>/longfellow/mdoc/circuits.zst > fixtures/nym-auth-circuits/nym_auth-ssc.bin   # gitignored
#   docker build -f dockerfiles/verifier-nymauth.Dockerfile \
#     --build-arg BASE=registry.fly.io/sirosid-gdc-vc-verifier:<the vc verifier build, zknative> \
#     -t registry.fly.io/sirosid-gdc-vc-verifier:local-<epoch> .
#
# A SECOND artifact sits beside it: the DECOMPRESSED Vega nym_auth R1CS shapes (12,205,144 bytes, +12 MB on the image),
# from the circuit catalog (https://zk-circuits-test.fly.dev/v1/circuits, entry nymauth-vega-mdoc-v1, compressed sha256
# b457ba5382eb7604ef035e790f2cb24f0cd5fdcd065f383ff1c2c49fab1bb4df); the verifier's shapes must be the very artifact the
# prover uses, so the digest below is the catalog's:
#
#   curl -s https://zk-circuits-test.fly.dev/v1/artifacts/sha256/b457ba5382eb7604ef035e790f2cb24f0cd5fdcd065f383ff1c2c49fab1bb4df \
#     | zstd -dc > fixtures/nym-auth-circuits/nym_auth-vega-ssc.bin   # gitignored
#
# The source archive is ours (no repository yet): ~/.cache/nymzip/nym-auth-circuits/longfellow/mdoc/circuits.zst
# (395,608 bytes, sha256 6c4b96ed431ee547a3a2e5b62621943a9a0a44646267653bf2db0fcc269b5c3b). Fetch it from its
# repository once there is one.
ARG BASE
FROM alpine:3.20 AS check
ARG ARTIFACT_BYTES=1062842
ARG ARTIFACT_SHA256=92dd42fdf99673249eec33c7d07850bd9da66fbdab01ef26391e04c8341c1b7f
ARG VEGA_BYTES=12205144
ARG VEGA_SHA256=4da5e73afb9be2682e70be793c9956a73304e21f31246aa10da0d3e95c926425
COPY fixtures/nym-auth-circuits/nym_auth-ssc.bin /nym_auth-ssc.bin
COPY fixtures/nym-auth-circuits/nym_auth-vega-ssc.bin /nym_auth-vega-ssc.bin
RUN set -e; \
    [ "$(wc -c < /nym_auth-ssc.bin)" = "${ARTIFACT_BYTES}" ] || { echo "artifact is not ${ARTIFACT_BYTES} bytes (decompressed?)"; exit 1; }; \
    echo "${ARTIFACT_SHA256}  /nym_auth-ssc.bin" | sha256sum -c -; \
    [ "$(wc -c < /nym_auth-vega-ssc.bin)" = "${VEGA_BYTES}" ] || { echo "vega artifact is not ${VEGA_BYTES} bytes (decompressed?)"; exit 1; }; \
    echo "${VEGA_SHA256}  /nym_auth-vega-ssc.bin" | sha256sum -c -

# Staged with its directories here: a distroless runtime has no shell to chmod with, and `COPY --chmod` gives the
# directories it creates the FILE's mode (0444: no x bit), which makes the artifact unreadable to the non-root user
# ("open /etc/vc/circuits/nym_auth-ssc.bin: permission denied" at startup).
RUN mkdir -p /out/etc/vc/circuits && cp /nym_auth-ssc.bin /out/etc/vc/circuits/nym_auth-ssc.bin \
    && cp /nym_auth-vega-ssc.bin /out/etc/vc/circuits/nym_auth-vega-ssc.bin \
    && chmod 0755 /out/etc /out/etc/vc /out/etc/vc/circuits \
    && chmod 0444 /out/etc/vc/circuits/nym_auth-ssc.bin /out/etc/vc/circuits/nym_auth-vega-ssc.bin

FROM ${BASE}
COPY --from=check /out/ /
