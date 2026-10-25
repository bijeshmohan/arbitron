# Shutdown

**Order policy.** Every order is an immediate-or-cancel limit order and is
final as soon as the venue answers. Nothing rests on the book, so there's
nothing to cancel on shutdown, and positions are left as they are. Tripping
the kill switch first is optional. It just makes sure no order starts while
you stop.

**Automatic.** On SIGTERM (`docker compose stop`, which waits up to 30 s), the
supervisor cancels its tasks and waits for them (with a timeout), delivers
pending alerts and releases the instance lock. A submission interrupted by
the cancellation is recorded as `SUBMISSION_UNKNOWN`. If the process is
killed outright, the order is left in `SUBMITTING` and the next startup marks
it unknown. Either way, it's resolved by reconciliation
([recovery](recovery.md)).

**Operator**

```sh
$C exec app arbitron killswitch trip --operator <you> --reason "planned stop"   # optional
$C stop app            # or: $C down  (keeps the volumes; never add -v)
```

For an upgrade, see `docs/deployment.md` → Upgrading. After a stop, follow
[startup](startup.md). If you tripped the switch, review and arm it as in
[kill-switch](kill-switch.md).

**Verify**

- `$C logs app | tail` shows "supervisor stopped" and no traceback.
- `$C exec app arbitron orders unresolved $CFG` (after restarting) is empty,
  or every entry resolves on the first reconciliation.
