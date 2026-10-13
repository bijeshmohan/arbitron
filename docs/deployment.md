# Deployment

arbitron runs as one Docker Compose stack on a single host, in **paper mode**.
Live trading stays disabled: the configuration refuses `mode = "live"`.

| Service      | What it does                                         | Host port        |
|--------------|------------------------------------------------------|------------------|
| `postgres`   | PostgreSQL 18, data in the `pgdata` volume           | none             |
| `migrate`    | applies migrations as the schema owner, then exits   | none             |
| `app`        | `paper run --follow`: trading, reconciliation, API   | `127.0.0.1:9108` |
| `backup`     | daily `pg_dump`, 14 days kept, in the `backups` volume | none           |
| `prometheus` | optional (`--profile monitoring`), 30 days of metrics | `127.0.0.1:9090` |
| `grafana`    | optional (`--profile monitoring`), starter dashboard | `127.0.0.1:3000` |

## Security model

- **Nothing is public.** Every port binds to the host's `127.0.0.1`. Reach
  them through an SSH tunnel (below). Don't change the bindings to `0.0.0.0`.
- **The API needs a bearer token** for everything except `GET /health`
  (a bare `{"status": "alive"}` for the container health check).
- **The API can only make things safer**: trip the kill switch and request a
  reconciliation. Reviewing, re-arming and resolving orders need the CLI,
  which needs shell access to the host.
- **Secrets are files** in `deploy/secrets/`, mounted into containers at
  `/run/secrets`. They never appear in environment variables,
  `docker inspect` output or logs. The directory is `0700`, so other host
  users can't read them.
- **Least-privilege database access.** Migrations run as the owner
  `arbitron`. The app connects as `arbitron_app`, which can read and write
  rows but can't create, alter, drop or truncate tables. (Truncating would get
  around the append-only triggers.)
- **Hardened containers.** The app runs as uid 10001 with a read-only root
  filesystem, all capabilities dropped and `no-new-privileges`. Base images
  are pinned by version and digest.

## First deployment

On the host (Docker with Compose v2, and a clone of this repository):

```sh
deploy/setup.sh
```

This creates random database passwords, the database URLs, the API token,
the Grafana admin password, empty Telegram placeholders, and
`deploy/arbitron.toml`. It never overwrites an existing file. Then:

1. Put your Telegram bot token and chat id (see the README) into
   `deploy/secrets/arbitron_telegram_bot_token` and
   `deploy/secrets/arbitron_telegram_chat_id`. The deployment config uses the
   `telegram` alert channel, and the app refuses to start without them.
2. Review `deploy/arbitron.toml`. The limits in it are examples, not
   recommendations.
3. Build and start the stack:

   ```sh
   export ARBITRON_REVISION="$(git rev-parse --short=12 HEAD)"  # recorded in backtests
   docker compose -f deploy/compose.yaml up -d --build
   docker compose -f deploy/compose.yaml ps        # app should become (healthy)
   docker compose -f deploy/compose.yaml logs -f app
   ```

4. Arm the kill switch. A fresh database starts in `recovery_review`, so
   nothing trades until a named operator arms it:

   ```sh
   docker compose -f deploy/compose.yaml exec app arbitron killswitch status
   docker compose -f deploy/compose.yaml exec app \
       arbitron killswitch arm --operator <you> --ack "<what you checked>"
   ```

Optional Prometheus and Grafana:

```sh
docker compose -f deploy/compose.yaml --profile monitoring up -d
```

## Reaching the API and dashboards

From your own machine:

```sh
ssh -N -L 9108:127.0.0.1:9108 -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 <host>
```

Then:

```sh
TOKEN="$(ssh <host> cat <repo>/deploy/secrets/arbitron_api_token)"
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9108/status
```

| Endpoint                       | Returns                                                 |
|--------------------------------|---------------------------------------------------------|
| `GET /health`                  | liveness (no token needed)                              |
| `GET /ready`                   | 200 only when safe to trade, otherwise 503 with reasons |
| `GET /status`                  | mode, readiness checks, kill switch, equity, tasks      |
| `GET /portfolio`               | balances, positions, equity                             |
| `GET /positions`               | positions                                               |
| `GET /orders?limit=N`          | recent orders (N up to 500, default 50)                 |
| `GET /signals?limit=N`         | recent signals                                          |
| `GET /risk/decisions?limit=N`  | recent risk decisions, with reasons and observed values |
| `GET /system/events?limit=N`   | recent audit events                                     |
| `GET /metrics`                 | Prometheus metrics                                      |
| `POST /kill-switch/trip`       | emergency stop; body `{"operator": "...", "reason": "..."}` |
| `POST /reconciliation`         | reconcile now; body `{"operator": "..."}`               |

Each operator action is recorded with actor `api:<operator>`, and a trip
sends a critical alert.

Grafana is at `http://127.0.0.1:3000` (user `admin`, password in
`deploy/secrets/grafana_admin_password`), with the "arbitron overview"
dashboard.

## Upgrading

```sh
git pull
export ARBITRON_REVISION="$(git rev-parse --short=12 HEAD)"
docker compose -f deploy/compose.yaml up -d --build
```

`migrate` runs before the app starts. If a migration fails, the app doesn't
start, and the old schema is left untouched. The app gets up to 30 s to shut
down cleanly. Startup recovery then reconciles before any trading.

## Backups and restore

`backup` dumps the database when it starts and every 24 hours after that. It
checks each dump is readable (`pg_restore --list`) and keeps 14 days of dumps.
To list them:

```sh
docker compose -f deploy/compose.yaml exec backup ls -lh /backups
docker compose -f deploy/compose.yaml logs backup   # "backup: FAILED" on errors
```

Dumps live in a Docker volume on the same host. That protects you from bad
data, but not from losing the disk. Copy them off the host regularly:

```sh
docker compose -f deploy/compose.yaml cp backup:/backups ./arbitron-backups
```

**Test a restore** at any time (it restores into a scratch database, checks
the schema revision and row counts, then drops it):

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps \
    --entrypoint /scripts/restore.sh backup verify /backups/arbitron-<stamp>.dump
```

**Replace the live database** from a backup:

```sh
docker compose -f deploy/compose.yaml stop app backup
docker compose -f deploy/compose.yaml run --rm --no-deps \
    --entrypoint /scripts/restore.sh backup replace /backups/arbitron-<stamp>.dump
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml exec app arbitron killswitch status
```

`replace` refuses to run while anything is connected. The old database is
kept as `arbitron_before_restore_<timestamp>`, so drop it with `dropdb` once
you're satisfied. After a restore, the app's startup reconciliation compares
the restored records with the paper venue's state.

**Disaster recovery on a new host:**

1. Run `setup.sh`, then copy over your old `deploy/secrets/` and
   `deploy/arbitron.toml`.
2. Start `postgres` and `backup`.
3. Copy the dump in with `docker compose cp <file> backup:/backups/`.
4. Run `replace` as above, then start the stack.

## Rotating secrets

- **API token:** write a new value (for example `openssl rand -hex 32`) to
  `deploy/secrets/arbitron_api_token`, then
  `docker compose -f deploy/compose.yaml up -d --force-recreate app prometheus`.
- **Telegram:** revoke the token with @BotFather, update the file, and
  recreate `app`.
- **Database passwords:** change the role's password in PostgreSQL
  (`ALTER ROLE arbitron_app PASSWORD '...'`), update `app_db_password` and
  `app_database_url` to match, then recreate `app`. Do the same for the
  owner with `postgres_password` and `owner_database_url`.

## Incidents

1. **Stop new orders first:**
   `curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"operator":"<you>","reason":"<why>"}' http://127.0.0.1:9108/kill-switch/trip`,
   or `docker compose ... exec app arbitron killswitch trip ...` on the host.
2. Look at `GET /status`, `GET /system/events`, `GET /orders`, and the app logs.
3. If secrets may have leaked, rotate them (above).
4. Re-arm only from the CLI, after
   `arbitron killswitch review` and with a written acknowledgement.

## Limits of this setup

- It's a single host, with no failover. If the host goes down, trading
  stops; the kill switch and startup reconciliation handle the restart.
- Backups stay on the same host until you copy them elsewhere.
- Nothing alerts you when a backup fails. Check `logs backup`, or watch the
  newest dump's age.
