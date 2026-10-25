# Operator runbooks

One page per situation (plan §23). Each one separates what the system does
by itself (**automatic**) from what only a person may do (**operator**), and
ends with how to verify the result.

| Runbook | When |
|---------|------|
| [startup](startup.md) | first start, and after any stop |
| [shutdown](shutdown.md) | planned stop or upgrade |
| [recovery](recovery.md) | after a crash, restart, unknown submission or discrepancy |
| [kill-switch](kill-switch.md) | stop trading now; re-arm afterwards |
| [exchange-outage](exchange-outage.md) | venue or network unreachable |
| [database-restore](database-restore.md) | verify backups; restore after data loss |
| [credential-rotation](credential-rotation.md) | replace keys or tokens, or suspected leak |
| [incident-response](incident-response.md) | anything unexpected involving orders or funds |

Every command assumes the deployment stack, run from the repository root:

```sh
C="docker compose -f deploy/compose.yaml"
CFG=/config/arbitron.toml            # the config path inside the app container
```

Two rules apply everywhere:

1. **When in doubt, trip the kill switch first.** Tripping only blocks new
   orders. It never closes positions or cancels anything, so it's always
   safe.
2. **Only a named operator re-arms**, from the CLI on the host, with a written
   acknowledgement. Both are recorded in `kill_switch_events`.
