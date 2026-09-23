# Backup, restore, RPO and RTO

§512–518, §651 ask for automatic backup, restore into a clean environment, and
documented RPO and RTO. This is the documentation, and the figures in it were
measured rather than estimated.

## Taking a backup

```bash
make backup            # or: ./ops/backup.sh
```

`pg_dump --format=custom --compress=6` inside the container, to
`backups/crpms-<UTC timestamp>.dump`, with `KEEP=7` generations retained. The
dump is written under a temporary name and renamed only when `pg_dump` has
succeeded, so a dump that failed half way is never mistaken for a backup or
counted towards retention (until 23 September it was). Every backup writes a row
to `audit_log`, so "when was the last good backup" is answerable from the same
place as everything else. The Docker CLI is found on PATH or inside Docker
Desktop (`ops/docker.sh`), or set with `DOCKER=`.

## Restoring into a clean environment

```bash
make restore DUMP=backups/crpms-20260922T192424Z.dump
```

The restore builds a **separate container and database** and never touches the
live one. It exits with `pg_restore`'s status, so a failed restore cannot be
reported as a success by anything that runs it (until 23 September it exited 0
whatever happened). A restore procedure that has only ever been run over the top of a
working system has not been tested: the failure it must survive is the system
being gone.

## Measured figures

| | 22 September | 23 September, re-verified after the schema changes |
|---|---|---|
| Archive | 10.15 million rows | 10.92 million rows |
| Backup duration | **8 s** | **12 s** |
| Backup size | **154.9 MB** (from a 1.1 GB database) | **167.9 MB** |
| **RTO** — restore into a clean container, verified | **20 s** | **36 s** |
| Rows restored | **10,155,600 of 10,155,600** | **10,921,597 of 10,921,597** samples up to the dump's newest; 336 of 336 audit rows |

The 23 September restore carried the append-only trigger on `audit_log`
(migration 015) and the new foreign keys across intact.

**RPO** is the backup interval. With the supplied hourly schedule the RPO is
**one hour**: up to an hour of samples can be lost if the archive is destroyed
between backups.

That figure deserves a caveat rather than a flourish. The collector's local
buffer holds unforwarded samples and replays them on reconnection, so an archive
outage loses nothing — that is Stage 3's measured result. The one-hour RPO
applies to the archive being *destroyed*, not merely unreachable, and only to
data the collector had already handed over and then discarded.

## Scheduling it

```bash
make backup-install     # launchd, hourly
make backup-uninstall
```

A LaunchAgent, so it runs at login rather than at boot; the same caveat as the
scraper applies.

## The defect this procedure had, and how it was found

The first version of `restore.sh` reported success, completed in 22 s, and
restored **8,943,924 of 10,154,800 samples**. It lost 1.2 million rows — about
12% of the archive — and said nothing.

Two mistakes compounded:

1. **TimescaleDB requires a restore to be bracketed** by
   `timescaledb_pre_restore()` and `timescaledb_post_restore()`. Those disable
   the chunk-routing machinery for the duration. Without them, rows destined for
   hypertable chunks are re-routed mid-load and silently go missing.
2. **The script threw away `pg_restore`'s stderr** with `2>/dev/null || true`.
   That is why the first mistake was invisible.

The second is the worse one. A restore that hides its complaints will hide any
future problem too, and a backup nobody has verified is a hope rather than a
control.

It was caught by comparing row counts between the source and the restored copy —
which is now part of the procedure and printed by the script, with the
instruction to compare them before trusting the backup.

**Verify every restore by count.** A dump that opens is not a dump that is
complete.

## What this does not do

- **No off-site copy.** A backup that lives on the machine it is backing up
  protects against a bad migration, not against the machine. Off-siting is a
  deployment decision and is not simulated here.
- **No encryption at rest.** The dump contains every measurement and the whole
  configuration.
- **No point-in-time recovery.** Restoring gets you to the last dump, not to an
  arbitrary instant. WAL archiving would change the RPO from an hour to seconds
  and is the obvious next step if the RPO matters.
