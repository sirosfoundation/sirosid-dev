# syntax=docker/dockerfile:1
# The sirosid-dev control plane (sirosid_service: console + API + MCP [+ assistant when
# its code and OPENROUTER_API_KEY are present]) as one image.
#
# Build context is the REPOSITORY ROOT (the root .dockerignore keeps generated
# secrets and local state out of it):
#
#   flyctl deploy . -c deploy/control-plane/fly.toml --dockerfile deploy/control-plane/Dockerfile --remote-only
#
# What the service needs at run time, and why it is here:
#   helm      sirosid_core.helm renders the chart (`helm template`) for every deploy
#   flyctl    sirosid_core.fly.FlyClient shells out to it for every Fly operation
#   openssl   fixtures/create-pki.sh and sirosid_core.api_auth
#   litestream  continuous SQLite replication to an S3-compatible bucket (entrypoint.sh)
#   chart/, values-*.yaml, fixtures/   Resources(root) - see sirosid_core/resources.py
#   scripts/bootstrap.py               registration with each new instance (__main__.py)
#   console/                           the web UI the service serves
# Every downloaded binary is pinned by version AND sha256; the build fails on a mismatch.
# x86_64 only (Fly machines). Bumping one: change both ARGs, from the project's release.

ARG PYTHON_IMAGE=python:3.13-slim@sha256:3dd7cc108ec1493442514f5c2a871af6af0ec31d768ff6e378a93340c3b3db5f

FROM ${PYTHON_IMAGE} AS tools
# https://github.com/helm/helm/releases  (get.helm.sh/helm-v<v>-linux-amd64.tar.gz.sha256sum)
ARG HELM_VERSION=3.21.4
ARG HELM_SHA256=61f88ab166748cb19604d7884cb100ae9ccb13804ddeb98e08af167eacbb6a14
# https://github.com/superfly/flyctl/releases  (flyctl_<v>_checksums.txt)
ARG FLYCTL_VERSION=0.4.111
ARG FLYCTL_SHA256=1878d7fb1f8a418039042cf0749e4b7216c6c7018b66ae10611a64c2b6d9aa9e
# https://github.com/benbjohnson/litestream/releases  (checksums.txt)
ARG LITESTREAM_VERSION=0.5.17
ARG LITESTREAM_SHA256=cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /dl
RUN set -eux; \
    curl -fsSLo helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz"; \
    echo "${HELM_SHA256}  helm.tgz" | sha256sum -c -; \
    tar -xzf helm.tgz linux-amd64/helm; \
    curl -fsSLo flyctl.tgz "https://github.com/superfly/flyctl/releases/download/v${FLYCTL_VERSION}/flyctl_${FLYCTL_VERSION}_Linux_x86_64.tar.gz"; \
    echo "${FLYCTL_SHA256}  flyctl.tgz" | sha256sum -c -; \
    tar -xzf flyctl.tgz flyctl; \
    curl -fsSLo litestream.tgz "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-x86_64.tar.gz"; \
    echo "${LITESTREAM_SHA256}  litestream.tgz" | sha256sum -c -; \
    tar -xzf litestream.tgz litestream; \
    mkdir -p /out; \
    install -m 0755 linux-amd64/helm flyctl litestream /out/; \
    /out/helm version --short; /out/flyctl version; /out/litestream version

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app
COPY --from=tools /out/helm /out/flyctl /out/litestream /usr/local/bin/

COPY deploy/control-plane/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/requirements.lock \
 && rm /tmp/requirements.lock

WORKDIR /app
# Resources(root) = /app: chart, values layers, fixtures (sirosid_core/resources.py).
COPY chart/ /app/chart/
COPY values-base.yaml values-dev.yaml values-fly.yaml /app/
COPY fixtures/ /app/fixtures/
COPY scripts/bootstrap.py /app/scripts/bootstrap.py
COPY console/ /app/console/
COPY sirosid_core/ /app/sirosid_core/
COPY sirosid_service/ /app/sirosid_service/
COPY deploy/control-plane/entrypoint.sh deploy/control-plane/litestream_config.py deploy/control-plane/sirosid-admin /app/deploy/
RUN chmod 0755 /app/deploy/entrypoint.sh /app/deploy/sirosid-admin \
 && ln -s /app/deploy/sirosid-admin /usr/local/bin/sirosid-admin \
 && python3 -c "from sirosid_core.resources import Resources; m = Resources('/app').missing(); assert not m, m" \
 && python3 -c "import sirosid_service.__main__"

ENV SIROSID_RESOURCES=/app \
    SIROSID_CONSOLE_DIR=/app/console \
    SIROSID_DB=/data/sirosid.db

EXPOSE 8080
# Starts as root ONLY to hand the volume's directory to `app`, then drops to it
# (entrypoint.sh); the service, Litestream and every flyctl/helm it runs are `app`.
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
