#!/bin/sh
# Backup loop (the `backup` service): a custom-format pg_dump now and then
# every BACKUP_INTERVAL_SECONDS, each checked readable with pg_restore --list,
# keeping BACKUP_RETENTION_DAYS of dumps in the `backups` volume.
set -eu

# A fresh volume is root-owned: fix ownership once, then drop to postgres.
if [ "$(id -u)" = 0 ]; then
    chown postgres:postgres /backups
    exec gosu postgres "$0" "$@"
fi

: "${BACKUP_INTERVAL_SECONDS:=86400}"
: "${BACKUP_RETENTION_DAYS:=14}"
export PGHOST=postgres PGUSER=arbitron PGDATABASE=arbitron
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD

# Every step checks its own status: `set -e` does not apply inside a function
# called from an `if`.
backup_once() {
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="/backups/arbitron-${stamp}.dump"
    pg_dump --format=custom --file="${target}.partial" || return 1
    pg_restore --list "${target}.partial" > /dev/null || return 1  # must be readable
    mv "${target}.partial" "${target}" || return 1
    echo "backup: wrote ${target} ($(du -h "${target}" | cut -f1))"
    find /backups -name 'arbitron-*.dump' -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete
    find /backups -name '*.partial' -mmin +60 -delete
}

while true; do
    if ! backup_once; then
        echo "backup: FAILED at $(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
    fi
    sleep "${BACKUP_INTERVAL_SECONDS}"
done
