# Recovery: restart, unknown submission, discrepancy

The rules are in `docs/failure-matrix.md`. This page covers what to do.

## After any restart or crash

**Automatic.** Startup recovery ([startup](startup.md)) marks interrupted
submissions as unknown and reconciles. A supervised task that crashes is
restarted with backoff, up to `task_max_restarts` times in
`task_restart_window_seconds`. One more crash trips the kill switch and stops
the session, and Docker then restarts the container.

**Operator.** Read the logs around the crash: `$C logs --since 1h app`. A
`supervisor.task_crashed` event is a bug, so file it with the traceback. If
the restart limit tripped the switch, follow [kill-switch](kill-switch.md) to
re-arm once the cause is understood.

## Locked out after a network failure

**Symptom.** The app restart-loops with `error: another paper session holds
this account's lock`, and no other arbitron instance is running.

**Cause.** The old process lost its network without closing its database
connections, so PostgreSQL still holds its single-instance lock. TCP
keepalives on the app and the server drop dead connections within about a
minute, so this should clear by itself. Before that fix, it took the OS default
of 2 hours (soak drill 2026-11-02).

**Operator, if it hasn't cleared within 5 minutes**

1. Make sure no other instance is running: `docker ps -a --filter name=arbitron-app`
   should show only the restart-looping container.
2. Find the lock holder and its sibling connections:

   ```sh
   $C exec postgres psql -U arbitron -d arbitron -c "
     select a.pid, a.client_addr, a.backend_start, a.state_change
     from pg_locks l join pg_stat_activity a using (pid) where l.locktype = 'advisory';"
   $C exec postgres psql -U arbitron -d arbitron -c "
     select pid, client_addr, backend_start, state_change from pg_stat_activity
     where usename = 'arbitron_app' order by backend_start;"
   ```

3. The dead process's connections all have a `backend_start` from **before**
   the outage and no activity since it began. Terminate **only those**, by PID:

   ```sh
   $C exec postgres psql -U arbitron -d arbitron -c "select pg_terminate_backend(<pid>);"
   ```

   Never terminate a connection unless you've confirmed its process is gone.
   The lock is what stops two instances trading at once.

**Verify:** `startup complete` with `reconciliation_clean: true` in the logs,
`/ready` returns 200, and a "Restarted after downtime" alert arrives.

## Unknown submission (`SUBMISSION_UNKNOWN`)

**Automatic.** New orders are blocked. Each reconciliation looks the order up
by client order ID:

- If the venue has it, its state and fills are applied.
- If it's missing on two lookups after the settle window, it becomes
  `NOT_PLACED`.
- If it's still unresolved after `unknown_order_max_age_seconds`, it becomes
  `RECONCILIATION_REQUIRED` and raises a critical alert.

**Operator (only for `RECONCILIATION_REQUIRED`)**

```sh
$C exec app arbitron orders unresolved $CFG
```

1. Look the order up on the venue yourself: by client order ID in the
   exchange's order history, or with `GET /orders` for paper.
2. If the venue has it, wait for the next reconciliation (or
   `POST /reconciliation`). It applies the venue's state automatically once
   the venue's state and trades agree.
3. If the venue has **no record** and you've checked, close it with the
   evidence:

   ```sh
   $C exec app arbitron orders resolve $CFG <order-id> --as not_placed \
       --operator <you> --note "<where you looked, what you saw>"
   ```

   `resolve` re-checks the venue and refuses if the order exists there.
   Never resolve an order that has fills.

## Discrepancy (kill switch in `recovery_review`)

**Automatic.** The reconciler never changes recorded fills or positions to
match the venue. It records a critical `reconciliation.discrepancy` and moves
an armed switch to review.

**Operator.** Read the discrepancy (`GET /system/events`, or the Telegram
alert). Then:

| Discrepancy | Likely cause | Action |
|-------------|--------------|--------|
| venue holds X, bot's position is Y | assets added to or removed from the account outside the bot; a missed fill | the account must be dedicated to the bot (Phase 12 decision); find the trade or transfer before re-arming |
| trade belongs to unknown order | a manual trade or another client on the account | stop that use of the account |
| unknown open order on the venue | a manual order | cancel it on the venue |
| trades sum to X but venue reports Y | venue inconsistency | wait for one or two reconciliations; escalate if it persists |

Re-arm only when the next reconciliation is clean (`GET /ready`, check
`reconciliation`). Then follow [kill-switch](kill-switch.md).

**Verify:** `orders unresolved` is empty, the latest reconciliation is clean,
and `/ready` returns 200.
