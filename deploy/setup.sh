#!/bin/sh
# One-time deployment setup: random secrets and the app config.
# Never overwrites an existing file, so it is safe to run again.
#
#   deploy/setup.sh
#   # then put your Telegram bot token and chat id into deploy/secrets/
set -eu

cd "$(dirname "$0")"
umask 077
mkdir -p secrets
chmod 700 secrets  # only the deploying user can reach the files on the host

random() { od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }

write() {  # write NAME VALUE: create once, readable inside the containers
    if [ ! -e "secrets/$1" ]; then
        printf '%s\n' "$2" > "secrets/$1"
        echo "created secrets/$1"
    fi
    # Containers run as other users (app 10001, prometheus 65534, grafana 472);
    # the 0700 directory keeps other host users out.
    chmod 644 "secrets/$1"
}

[ -e secrets/postgres_password ] || write postgres_password "$(random)"
[ -e secrets/app_db_password ] || write app_db_password "$(random)"
owner="$(head -n1 secrets/postgres_password)"
app="$(head -n1 secrets/app_db_password)"
write owner_database_url "postgresql+psycopg://arbitron:${owner}@postgres:5432/arbitron"
write app_database_url "postgresql+psycopg://arbitron_app:${app}@postgres:5432/arbitron"
write arbitron_api_token "$(random)"
write grafana_admin_password "$(random)"
write arbitron_telegram_bot_token ""
write arbitron_telegram_chat_id ""

if [ ! -e arbitron.toml ]; then
    cp arbitron.example.toml arbitron.toml
    chmod 644 arbitron.toml
    echo "created arbitron.toml (review the limits before starting)"
fi

if [ ! -s secrets/arbitron_telegram_bot_token ] || [ ! -s secrets/arbitron_telegram_chat_id ]; then
    echo
    echo "next: put your Telegram bot token and chat id into"
    echo "  deploy/secrets/arbitron_telegram_bot_token"
    echo "  deploy/secrets/arbitron_telegram_chat_id"
fi
