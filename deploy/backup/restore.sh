#!/bin/sh
# Restore a backup made by backup.sh. Run inside the backup image:
#
#   docker compose -f deploy/compose.yaml run --rm --no-deps \
#       --entrypoint /scripts/restore.sh backup verify /backups/arbitron-<stamp>.dump
#
#   verify  <dump>  restore into a scratch database, check it, drop it (safe any time)
#   replace <dump>  restore, check, then swap it in for the live database; the old one
#                   is kept as arbitron_before_restore_<timestamp>. Stop app and backup first.
set -eu

usage() {
    echo "usage: restore.sh verify|replace /backups/arbitron-<stamp>.dump" >&2
    exit 2
}
[ $# -eq 2 ] || usage
mode="$1"
dump="$2"
case "$mode" in verify | replace) ;; *) usage ;; esac
[ -f "$dump" ] || { echo "restore: no such file: $dump" >&2; exit 2; }

export PGHOST=postgres PGUSER=arbitron PGDATABASE=postgres
PGPASSWORD="$(cat /run/secrets/postgres_password)"
export PGPASSWORD

scratch="arbitron_restore_$(date -u +%Y%m%d%H%M%S)"

q() { psql -v ON_ERROR_STOP=1 -tAc "$1" "${2:-postgres}"; }

echo "restore: restoring $dump into $scratch"
createdb --owner=arbitron "$scratch"
if ! pg_restore --exit-on-error --dbname="$scratch" "$dump"; then
    dropdb "$scratch"
    echo "restore: FAILED; live database untouched" >&2
    exit 1
fi

revision="$(q "SELECT version_num FROM alembic_version" "$scratch")"
echo "restore: schema revision $revision"
for table in candles signals orders fills positions equity_snapshots system_events backtest_runs; do
    echo "restore:   $table: $(q "SELECT count(*) FROM $table" "$scratch") rows"
done

if [ "$mode" = verify ]; then
    dropdb "$scratch"
    echo "restore: verified OK (scratch database dropped)"
    exit 0
fi

active="$(q "SELECT count(*) FROM pg_stat_activity WHERE datname = 'arbitron'")"
if [ "$active" != 0 ]; then
    dropdb "$scratch"
    echo "restore: $active connection(s) to arbitron; stop app and backup first:" >&2
    echo "  docker compose -f deploy/compose.yaml stop app backup" >&2
    exit 1
fi

old="arbitron_before_restore_$(date -u +%Y%m%d%H%M%S)"
psql -v ON_ERROR_STOP=1 -q postgres <<SQL
ALTER DATABASE arbitron RENAME TO ${old};
ALTER DATABASE ${scratch} RENAME TO arbitron;
REVOKE ALL ON DATABASE arbitron FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE arbitron TO arbitron_app;
SQL
echo "restore: arbitron replaced; previous database kept as ${old}"
echo "restore: start the app again (docker compose ... up -d), then check: arbitron killswitch status"
