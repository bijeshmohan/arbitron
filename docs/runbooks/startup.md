# Startup

**Automatic** (`paper run --follow`, plan §14.1). Trading is enabled only
after all of these:

1. Configuration validated. Live mode, and any `[live]` flag, is refused.
2. Telegram credentials and the API token are present. Otherwise the app
   refuses to start.
3. The database schema is at head. `migrate` runs first, and the app doesn't
   start if it fails.
4. The single-instance lock is acquired. A second instance exits.
5. The kill switch is initialized. A fresh database starts in
   `recovery_review`.
6. Market metadata is loaded, orders stuck in `SUBMITTING` are marked
   `SUBMISSION_UNKNOWN`, and a full reconciliation runs. Any discrepancy moves
   an armed switch to review.

**Operator**

```sh
$C up -d --build
$C ps                                   # app becomes (healthy)
$C logs -f app                          # "startup complete", reconciliation_clean
$C exec app arbitron killswitch status
```

If the switch is in `recovery_review`, which it always is on a fresh
database, check the evidence below and then arm it:

```sh
$C exec app arbitron orders unresolved $CFG     # must be empty
$C exec app arbitron killswitch arm --operator <you> --ack "<what you checked>"
```

**Verify**

- `GET /ready` returns 200. Every check is `ok`: startup, kill_switch,
  reconciliation, market_data, tasks.
- `arbitron alerts test $CFG` delivers to Telegram.
- After the next candle close, `GET /status` shows a recent cycle.
