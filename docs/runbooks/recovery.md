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
