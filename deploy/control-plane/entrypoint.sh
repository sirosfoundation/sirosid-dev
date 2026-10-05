#!/bin/sh
# The control plane's entrypoint (deploy/control-plane/Dockerfile).
#
#   root:  make the volume's directory the service user's, then re-exec as that user
#          (Fly mounts a fresh volume root-owned; the service never runs as root)
#   user:  write the Litestream config from the environment (litestream_config.py),
#          restore the database from the replica if the volume has none, then run the
#          service UNDER `litestream replicate -exec`, so every write is shipped and a
#          stop flushes the last of the WAL before exit.
#
# Restore is fail-closed: a missing database with a configured replica that cannot be
# restored stops the boot, because starting empty would hand out a fresh bootstrap
# admin invite and orphan every instance. The ONE way to start empty is the explicit
# first-boot flag SIROSID_FIRST_BOOT=1 (pass it to the first deploy only:
# `fly deploy -e SIROSID_FIRST_BOOT=1`); even then an existing replica is restored.
#
# Overridable for tests: LITESTREAM_BIN, PYTHON, SIROSID_SERVE_CMD, SIROSID_RUN_AS.
set -eu

DB="${SIROSID_DB:-/data/sirosid.db}"
LITESTREAM_BIN="${LITESTREAM_BIN:-litestream}"
PYTHON="${PYTHON:-python3}"
SERVE="${SIROSID_SERVE_CMD:-$PYTHON -m sirosid_service serve}"
RUN_AS="${SIROSID_RUN_AS:-app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${LITESTREAM_CONFIG:-${TMPDIR:-/tmp}/litestream.yml}"

log() { echo "entrypoint: $*" >&2; }
die() { echo "entrypoint: FATAL: $*" >&2; exit 1; }

if [ "$(id -u)" = 0 ]; then
    dir="$(dirname "$DB")"
    mkdir -p "$dir"
    chown "$RUN_AS:$RUN_AS" "$dir"
    # A database (and its -wal/-shm) touched by root over `fly ssh console` must stay
    # writable for the service.
    for f in "$DB" "$DB-wal" "$DB-shm"; do
        if [ -e "$f" ]; then chown "$RUN_AS:$RUN_AS" "$f"; fi
    done
    exec setpriv --reuid="$RUN_AS" --regid="$RUN_AS" --init-groups env HOME="/home/$RUN_AS" "$0" "$@"
fi

# The replica settings are the names `fly storage create -a <app>` sets (BUCKET_NAME,
# AWS_*); the config file only references the keys, Litestream expands them itself.
rc=0
"$PYTHON" "$HERE/litestream_config.py" "$CONF" || rc=$?
case "$rc" in
    0) ;;
    10)
        if [ "${SIROSID_REQUIRE_REPLICA:-0}" = 1 ]; then die "SIROSID_REQUIRE_REPLICA=1 but no replica is configured"; fi
        log "WARNING: no Litestream replica configured - the database at $DB is NOT backed up"
        # eval: the command is one string (as `litestream -exec` takes it), quotes included.
        eval "exec $SERVE"
        ;;
    *) die "the Litestream replica is misconfigured (see above); refusing to run without the backup it asks for" ;;
esac

if [ ! -e "$DB" ]; then
    if [ "${SIROSID_FIRST_BOOT:-0}" = 1 ]; then
        log "no database at $DB; SIROSID_FIRST_BOOT=1: restoring the replica if there is one, else starting empty"
        "$LITESTREAM_BIN" restore -config "$CONF" -if-replica-exists "$DB" \
            || die "litestream restore failed (with SIROSID_FIRST_BOOT=1, only a MISSING replica is tolerated)"
    else
        log "no database at $DB: restoring it from the replica"
        "$LITESTREAM_BIN" restore -config "$CONF" "$DB" \
            || die "litestream restore failed and SIROSID_FIRST_BOOT is not set. If this really is the first boot, deploy once with -e SIROSID_FIRST_BOOT=1; otherwise fix the replica settings - starting empty would orphan every instance"
    fi
    if [ -e "$DB" ]; then log "restored $DB"; else log "no replica yet: starting with an empty database"; fi
fi

exec "$LITESTREAM_BIN" replicate -config "$CONF" -exec "$SERVE"
