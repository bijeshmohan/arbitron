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
| 8 | Task crashes and downtime | 0 supervisor events and 0 restarts after a gap of more than 5 minutes, outside the drills | no unhandled errors or unplanned outages |
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
| 8 | Network outage: `docker network disconnect arbitron_default arbitron-app-1` for 10 min, then `docker compose -f deploy/compose.yaml up -d --no-build --no-deps --force-recreate app` (a plain `network connect` fails while the container is restart-looping) | the app loses the venue *and* its database, the session stops without placing orders (failure matrix #5, #15) and Docker restarts it; once reconnected, startup reconciliation is clean and cycles resume. The Telegram alert may only arrive after reconnecting. |
| 9 | Database restore: `restore.sh verify` on the newest dump | the restore succeeds and the row counts are printed |

## Confirmation run after the soak

The day-8 network drill found that recovery from a full network loss wasn't
automatic, and that the outage raised no alert. Following the operator's
decision (2026-11-02, option A), the fixes are developed during the soak but
deployed only after it ends, so the 14-day evidence stays on one build:

- TCP keepalives on the app's database connections and on PostgreSQL, so dead
  connections, and a dead process's lock, are dropped in about a minute;
- a 10 s bound on the lock check, so a lock that can't be confirmed counts as
  lost;
- a critical "Restarted after downtime" alert when the app restarts more than
  5 minutes after its last heartbeat, which is also counted by criterion 8.

After 2026-11-08: deploy (this restarts `postgres` and `app`), repeat the
day-8 network drill, and run a 3-day confirmation soak with the same
criteria. Pass means the app recovers from the outage by itself within a few
minutes, the alert arrives, and the confirmation run passes.

## Log

Fill this in during the soak (UTC).

| When | What | Result / notes |
|------|------|----------------|
| 2026-10-24 08:03 | stack started (image from 2635989 plus the uncommitted Phase 12 tooling; trading code unchanged) | healthy; startup reconciliation clean; paper account funded 1000 USDT; first cycle quality OK |
| 2026-10-24 08:04 | drill day 1: `arbitron alerts test` | sent via Telegram; received by the operator (confirmed) |
| 2026-10-25 | pre-window check (unarmed, 29 h) | healthy, cycles every 5 min, no errors, daily backups on 10-24 and 10-25; host confirmed as this server |
| 2026-10-25 12:45:29 | kill switch armed by Bijesh Mohan: **window start**; window ends 2026-11-08 12:45:29 | `/ready` 200, all checks ok |
| 2026-10-27 01:40 | status check (1 d 13 h): soak report PASS | root disk 92% (3.7 GB free), down from 7.8 GB, caused by another project on the host (a 3.45 GB Playwright image); arbitron growth negligible. Watch: a full disk would stop PostgreSQL. |
| 2026-10-27 01:56:02 | drill day 2: graceful restart (`docker compose restart app`) | **pass**: clean stop (6 tasks stopped, 0 stuck); healthy again at 01:56:10; startup reconciliation clean, 0 recovered orders; cycles resumed at the 02:00 close; `/ready` 200; no unresolved orders. Disk at 81% after the operator's cleanup. Found and fixed a soak-report bug (a cycle in the unfinished interval was counted, showing 447/446); the fix goes into the image at the day-4 rebuild. |
| 2026-10-29 00:36:18 | drill day 4: hard kill (`docker compose kill app`, exit 137), then `up -d` on an image rebuilt from 1abcb52 | **pass**: healthy again at 00:36:30; startup reconciliation clean, 0 recovered orders, instance lock reacquired; cycles resumed at the 00:40 close; `/ready` 200; no unresolved orders; 0 critical events. The soak-report fix is now live (1008/1008). Disk 84% (7.3 GB free). |
| 2026-10-31 04:33:46 | drill day 6: kill-switch trip via `POST /kill-switch/trip` (`api:bijesh.mohan`); re-armed from the CLI by Bijesh Mohan at 04:35:59 (review 04:35:58) | **pass**: `/ready` 503 while tripped; the `kill_switch.tripped` critical event sent a Telegram alert at 04:33:50 (4 s; received, confirmed by the operator); 0 orders while blocked; `/ready` 200 after arming. Finding: the API's operator field rejects spaces (`^[A-Za-z0-9_.@-]{1,64}$`) while the CLI accepts them, so one person appears as both `Bijesh Mohan` and `api:bijesh.mohan` in the audit trail. Cosmetic; noted for the review. |
| 2026-11-02 03:21:04 | drill day 8: network outage (`docker network disconnect arbitron_default arbitron-app-1` for 10 min) | **safe, but did not recover by itself**. No orders during the outage; the reconciliation task failed on the unreachable database at 03:21:34; the process exited at about 03:25 and Docker restart-looped it (exit 2, database unreachable). Findings: (1) `docker network connect` failed at 03:31 because the container was mid-restart; recovered with `up -d --force-recreate app`. (2) **The app was then locked out** (`another paper session holds this account's lock`): PostgreSQL still held the old process's session advisory lock on a dead TCP connection, and with the OS keepalive at 7200 s it would have held it until about 05:20. Recovered at 04:10:49 by terminating the 4 dead backends (all from the old process, started 2026-10-29 00:36, silent since 03:21); healthy at 04:10:58, startup reconciliation clean, `/ready` 200, cycles resumed 04:15. (3) **No Telegram alert at all**: with the database down, no critical event could be recorded, and after recovery nothing recorded the outage, so the soak's task-crash criterion also shows 0 for a real crash. Gap 03:20 to 04:11 (50 min); cycle coverage 99.56%. |
| 2026-11-03 02:16:34 | drill day 9: database restore (`restore.sh verify` on `arbitron-20261102T080308Z.dump`) | **pass**: restored into a scratch database at schema revision 29b951c26216 (the live head); candles 3592, signals 105, orders 95, fills 86, positions 1, equity snapshots 2673, system events 25. The 86 fills match the 86 filled orders (the other 9 orders are the pre-window `kill_switch_not_armed` rejections). Scratch database dropped; the live stack was untouched. |
| 2026-11-08 12:45:29 | **window end; final result** (`soak report --start 2026-10-25T12:45:29Z --end 2026-11-08T12:45:29Z`, scored 2026-11-09) | **PASS.** Criteria 1–10 pass (criterion 11 isn't checked for a past window): cycles 4022/4032 (99.75%; the only gap is the 50-minute day-8 drill), candles 4031/4031, 163 orders all filled, fills match, 0 venue rejections, 0 discrepancies, 0 ambiguous submissions, 0 automatic kill-switch faults, armed at the end; 1 critical event (the day-6 drill trip). Operator checks: 15 daily backups with 0 failures (12); alerts delivered and received on 10-24 and 10-31 (13); restore drill passed on 11-03 (14); app memory flat at 336–360 MB of 768 MB, disk 71% (15). **Caveat:** criterion 8 reads 0, but the day-8 outage crashed a task that could not be recorded with the database down. That blind spot and the manual recovery are fixed in 904d850, which is still to be deployed and confirmed (see Confirmation run). |
| | | |
