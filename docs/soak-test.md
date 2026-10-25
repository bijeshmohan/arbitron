# Paper soak test (Phase 12)

A sustained, unattended paper run on the deployment stack, judged against
criteria fixed **before** it starts. It tests whether the system runs,
recovers and stays consistent. It does not test whether the strategy makes
money: no criterion depends on profit or loss, and a profitable soak is not
evidence of future returns.

- **Duration:** 14 days of continuous running (operator decision 2026-10-24).
- **Where:** this host, using `deploy/compose.yaml` in paper mode with
  Telegram alerts.
- **Config:** `deploy/arbitron.toml`. Its risk limits are the example values,
  and they don't matter for paper funds. Don't change the config during the
  soak. A config change restarts the 14 days.
- **Window:** from the moment the kill switch is first armed to that moment
  plus 14 days. Record both in the log at the bottom.

## Pass criteria

`arbitron soak report` scores the database-backed criteria over the window:

```sh
docker compose -f deploy/compose.yaml exec app \
    arbitron soak report /config/arbitron.toml --start <armed-at> [--end <armed-at + 14d>]
```

| # | Criterion | Threshold | Why |
|---|-----------|-----------|-----|
| 1 | Trading cycles | a cycle (equity snapshot) in ≥ 99% of 5-minute intervals | the loop keeps running, and restarts are short |
| 2 | Closed candles per symbol | ≥ 99% of expected candles stored | ingestion keeps up and backfills after gaps |
| 3 | Orders settled | every order older than 15 min is terminal | nothing is left ambiguous |
| 4 | Fills match orders | every terminal order's fills sum to its filled amount | accounting integrity |
| 5 | No venue rejections | 0 `exchange_rejected` | sizing respects venue rules |
| 6 | Reconciliation discrepancies | 0 | the bot's records agree with the venue |
| 7 | Ambiguous submissions | 0 `submission_unknown` outside the drills | submissions get definite answers |
| 8 | Task crashes | 0 supervisor events outside the drills | no unhandled errors |
| 9 | No automatic kill-switch faults | 0 trips or reviews by the system other than a loss-limit trip | fail-closed paths only fire for real reasons |
| 10 | Kill switch armed at the end | `armed` | the run ends in a trading-ready state |
| 11 | Tasks alive at the end | every heartbeat less than 5 min old (only checked when the window ends now) | nothing has silently died |

Criteria 7 and 8 exclude events caused deliberately by the drills below. Note
each drill's time in the log, so its events can be matched. A loss-limit
trip is reported but doesn't fail the soak, because it means the risk engine
worked. It does need the operator to review and re-arm (criterion 10).

### Checked by the operator

The database cannot show these, so record them in the log:

| # | Check | Threshold |
|---|-------|-----------|
| 12 | Daily backups | at least 13 dumps over the 14 days, none `FAILED` (`docker compose ... logs backup`) |
| 13 | Alert delivery | each drill that raises a critical event reaches Telegram within 5 minutes |
| 14 | Restore drill | `restore.sh verify` succeeds on a dump from the soak |
| 15 | Resource use | the app stays well under its 768 MB memory limit, with no steady climb, and disk use grows predictably (`docker stats`, `df`) |

**Pass** means criteria 1–11 pass in `soak report` and checks 12–15 are
recorded as passing. Any failure is investigated and written up. Whether it
needs a fix and a fresh 14-day run is decided at the Phase 12 review.

## Drills during the soak

Do them during the first 10 days, at least a day apart. Each one has a
runbook in `docs/runbooks/`:

| Day | Drill | Expected result |
|-----|-------|-----------------|
| 1 | `arbitron alerts test` | the Telegram message arrives |
| 2 | Graceful restart: `docker compose -f deploy/compose.yaml restart app` | startup reconciliation is clean, cycles resume, nothing is ambiguous |
| 4 | Hard kill: `docker compose -f deploy/compose.yaml kill app`, then `up -d` | as above; any order caught mid-submission is resolved automatically (failure matrix #6) |
| 6 | Kill-switch drill: trip it through the API, check `/ready` returns 503, then review and re-arm from the CLI | the trip alert arrives, no orders while tripped, and re-arming needs a named operator |
| 8 | Network outage: `docker network disconnect arbitron_default arbitron-app-1` for 10 min, then `docker network connect arbitron_default arbitron-app-1` | the app loses the venue *and* its database, the session stops without placing orders (failure matrix #5, #15) and Docker restarts it; once reconnected, startup reconciliation is clean and cycles resume. The Telegram alert may only arrive after reconnecting. |
| 9 | Database restore: `restore.sh verify` on the newest dump | the restore succeeds and the row counts are printed |

## Log

Fill this in during the soak (UTC).

| When | What | Result / notes |
|------|------|----------------|
| 2026-10-24 08:03 | stack started (image from 2635989 plus the uncommitted Phase 12 tooling; trading code unchanged) | healthy; startup reconciliation clean; paper account funded 1000 USDT; first cycle quality OK |
| 2026-10-24 08:04 | drill day 1: `arbitron alerts test` | sent via Telegram; received by the operator (confirmed) |
| 2026-10-25 | pre-window check (unarmed, 29 h) | healthy, cycles every 5 min, no errors, daily backups on 10-24 and 10-25; host confirmed as this server |
| 2026-10-25 12:45:29 | kill switch armed by Bijesh Mohan: **window start**; window ends 2026-11-08 12:45:29 | `/ready` 200, all checks ok |
| | | |
