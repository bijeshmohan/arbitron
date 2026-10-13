#!/bin/sh
# Runs once, when the PostgreSQL volume is first initialized.
#
# Least privilege (plan §18): the schema owner `arbitron` runs migrations;
# the application connects as `arbitron_app`, which may read and write rows
# in tables the owner creates but cannot create, alter, drop, or truncate
# anything. TRUNCATE in particular would bypass the append-only triggers.
set -eu

APP_PASSWORD="$(cat "$ARBITRON_APP_PASSWORD_FILE")"
if [ -z "$APP_PASSWORD" ]; then
    echo "init-roles: app_db_password is empty" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -v app_password="$APP_PASSWORD" \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE arbitron_app LOGIN PASSWORD :'app_password';
REVOKE ALL ON DATABASE arbitron FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE arbitron TO arbitron_app;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO arbitron_app;
ALTER DEFAULT PRIVILEGES FOR ROLE arbitron IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO arbitron_app;
ALTER DEFAULT PRIVILEGES FOR ROLE arbitron IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO arbitron_app;
SQL
