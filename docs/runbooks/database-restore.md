# Database backup verification and restore

The commands and details are in `docs/deployment.md` → Backups and restore.
This page covers when and how to run them.

**Automatic.** `backup` dumps the database at start and every 24 h, checks
that each dump is readable (`pg_restore --list`) and keeps 14 days of dumps.
Failures appear as `backup: FAILED` in `$C logs backup`. **They don't
alert**, so check them.

**Operator: weekly verification**

```sh
$C exec backup ls -lh /backups                     # newest dump < 25 h old
$C run --rm --no-deps --entrypoint /scripts/restore.sh backup verify /backups/<newest>.dump
$C cp backup:/backups ./arbitron-backups           # copy off the host
```

`verify` restores into a scratch database, checks the schema revision and
row counts, then drops it. The live database isn't touched.

**Operator: restore after data loss or corruption**

1. Trip the kill switch if the app is still running ([kill-switch](kill-switch.md)).
2. Pick the newest good dump. Anything after it is lost from the bot's
   records, **but not from the venue**.
3. Replace the database:

   ```sh
   $C stop app backup
   $C run --rm --no-deps --entrypoint /scripts/restore.sh backup replace /backups/<dump>
   $C up -d
   ```

4. Startup reconciliation now compares the restored records with the venue.
   Orders and fills placed after the dump are on the venue but not in the
   database, so expect discrepancies: unknown trades and holdings mismatches.
   That's correct. **Don't re-arm** until each one is explained. On a live
   venue, every trade after the dump has to be accounted for by hand before
   trading resumes.
5. Drop the old database (`arbitron_before_restore_<timestamp>`) once you're
   satisfied.

**Verify:** `$C exec app arbitron db status` is at head, `paper status`
shows the expected balances and positions, and the first reconciliation's
discrepancies are all explained.
