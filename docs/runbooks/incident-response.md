# Incident response

An incident is anything unexpected that touches orders, positions, funds,
credentials or the host. Examples: an alert you can't explain, orders you
didn't expect, balances that don't match, a suspected leak, a host
compromise.

## 1. Stop new trading (seconds)

Trip the kill switch ([kill-switch](kill-switch.md)). This is always safe: it
blocks new orders and changes nothing else.

**Don't** stop the app, delete data or restore a backup yet. The running
system keeps reconciling, and its records are the evidence.

## 2. Preserve evidence (minutes)

```sh
mkdir -p incident-$(date -u +%Y%m%dT%H%M%SZ) && cd $_
$C logs --since 48h app > app.log
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:9108/system/events?limit=500' > events.json
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:9108/orders?limit=500' > orders.json
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9108/status > status.json
$C restart backup        # takes a fresh, verified dump on start; then copy it off:
$C cp backup:/backups ./backups
```

On a live venue, also export the exchange's order and trade history for the
same period.

## 3. Contain

- **Credentials might be exposed:** rotate them now
  ([credential-rotation](credential-rotation.md)), starting with the exchange
  key. Delete it on the exchange, don't just stop using it.
- **The host might be compromised:** revoke the exchange key from another
  device first, then take the host offline.
- **Unexpected open orders on the venue:** cancel them on the venue.

## 4. Investigate

Compare the bot's records (orders, fills, `system_events`, `order_events`)
with the venue's history. Every order and fill has a correlation ID, and every
order's state changes are in `order_events`. Write down the timeline, the
cause and the effect on positions and funds.

## 5. Resume criteria

Resume only when all of these hold:

- the cause is understood, and either fixed or explicitly accepted in
  writing;
- `orders unresolved` is empty and the latest reconciliation is clean;
- credentials are rotated if there was any doubt about them;
- the operator re-arms with an acknowledgement that cites the write-up.

## 6. Afterwards

Add a failure-injection test or a `docs/failure-matrix.md` row if the system
behaved unexpectedly. Update the runbook that was missing a step.
