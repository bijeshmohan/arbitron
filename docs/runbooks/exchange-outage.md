# Exchange outage or network loss

**Automatic**

- **Reads** (market data, balances, reconciliation) are retried with backoff
  on timeouts, rate limits and 5xx errors. A cycle that still fails is
  skipped and logged, and it doesn't count as a crash.
- **Stale data:** the quality gate blocks new orders once candles or the
  ticker are older than `max_market_data_age_seconds` /
  `max_ticker_age_seconds` (failure matrix #11).
- **Submissions** are never retried. An ambiguous one becomes
  `SUBMISSION_UNKNOWN` and is resolved by reconciliation once the venue is
  back ([recovery](recovery.md)).
- **Reconciliation that can't reach the venue** stays "not clean", so orders
  remain blocked (failure matrix #16).
- **If the database is also unreachable** (a full network loss), the
  instance lock is lost and the session stops. Docker restarts it, and
  startup recovery runs once the network is back (failure matrix #5, #15).

**Operator**

1. Check whether it's the venue or your side: try
   `curl -s https://api.binance.com/api/v3/time` from the host, and look at
   the Binance status page or announcements for maintenance.
2. Nothing needs doing while it lasts. Trading resumes by itself when data
   is fresh and reconciliation is clean.
3. If the outage covers an order submission, or lasts longer than
   `unknown_order_max_age_seconds`, expect `RECONCILIATION_REQUIRED` and
   follow [recovery](recovery.md).
4. For a long or repeated outage, trip the kill switch ([kill-switch](kill-switch.md))
   so trading doesn't resume unattended in odd conditions.

**Verify after it ends:** `GET /ready` returns 200 (market_data and
reconciliation are `ok`), `GET /status` shows cycles resuming, and
`orders unresolved` is empty.
