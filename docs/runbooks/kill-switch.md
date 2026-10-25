# Kill switch: trip, verify, acknowledge, re-arm

States: `armed` (trading allowed), `tripped` (new orders blocked), and
`recovery_review` (blocked until a named operator re-arms). It only blocks
**new** orders. It never cancels orders or closes positions.

**Automatic trips and reviews**

- The daily loss or drawdown limit is breached: tripped.
- An authentication failure on submission: tripped.
- A critical task hits its restart limit: tripped, and the session stops.
- A reconciliation discrepancy: review.
- A fresh database: starts in review.

Every one of these is a critical event and alert.

**Operator: trip**

```sh
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    -d '{"operator":"<you>","reason":"<why>"}' http://127.0.0.1:9108/kill-switch/trip
# or, on the host:
$C exec app arbitron killswitch trip --operator <you> --reason "<why>"
```

**Verify the trip**

- `arbitron killswitch status` shows `tripped (new orders BLOCKED)`.
- `GET /ready` returns 503 with `kill_switch: kill switch tripped`.
- A Telegram alert arrives.
- No new orders appear in `GET /orders` after the trip time.

**Operator: re-arm.** This is CLI only. The API can't re-arm.

```sh
$C exec app arbitron killswitch review --operator <you> --reason "<investigating>"
# fix the cause; confirm `orders unresolved` is empty and the last reconciliation is clean
$C exec app arbitron killswitch arm --operator <you> --ack "<cause and evidence>"
```

`arm` is refused unless the switch is in `recovery_review`. The
acknowledgement is stored permanently, so write down what you checked, not
just "ok".

**Verify:** `/ready` returns 200, and `kill_switch_events` (or
`GET /system/events`) shows your trip, review and arm with your name.
