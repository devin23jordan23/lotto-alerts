# Database lifetime, backup, and rollback

This note is the operator procedure for the Nov 9 durability change. It does not
change live order routing. Nothing here deletes a database.

## What failed on Nov 6

The Nov 6 capture is a new SQLite file. Its first rows are 10:40 ET, and the
open is not in that file. The worker creates `DATA_DIR/live.sqlite3` when the
file is absent. If a redeploy comes up without the volume mounted, that path is
on the container disk, the new file is empty, and the morning that was written
on the previous disk is gone. The old process did not delete rows. It started a
different file.

`session` and `nightly` now refuse a missing file instead of creating one.
Live mode refuses to create one during the weekday 09:30–16:00 ET clock.

Railway's own reference says `RAILWAY_VOLUME_MOUNT_PATH` and
`RAILWAY_VOLUME_NAME` are both injected only when a volume is attached. This
repo's `railway.json` does not declare that volume. A path that merely starts
with the mount string is not treated as a volume. Live mode also requires the
directory to be a real mount (a different device from its parent, or an entry
in `/proc/self/mountinfo`). `LOTTO_TRUST_VOLUME_MOUNT=1` skips that mount check
and is an unverified override. It still rejects a database path outside the
configured mount path. The production service's volume name and mount were not
inspected from this repo, so that attachment remains a rollout prerequisite.

## What a startup does to an existing file

An existing file is opened read-only first. If its schema version is newer than
this build, the process stops before any writable connection. Nothing is
deleted. A recognized file is then opened for write and only adds tables that
are not already there (`schema_meta`, `runtime_starts`) and records schema
version 2. It adds the older `phase`, `parent_id`, and `thesis_id` alert
columns only when those columns are absent. It appends one `runtime_starts`
row in live mode. It does not drop tables, delete rows, or replace the file.

`python -m lotto.main migrate --db PATH` is the same open, plus a printed inode
check. `session`, `backup`, and a refused `nightly` open the file read-only and
do not migrate it.

## Backup

Stop the worker, or run the backup command while it is up. The command uses
SQLite's backup API and will not overwrite the destination.

```bash
python3 -m lotto.main backup --db /app/data/live.sqlite3 --dest /app/data/backups/live-before-change.sqlite3
```

If this build is not on the box yet, copy the file and any `live.sqlite3-wal`,
`live.sqlite3-shm`, or `live.sqlite3-journal` beside it while the worker is
stopped. Do not copy those sidecars onto a file that a live process is writing.
Keep the copy. Do not delete the source to "make room".

Confirm the file you backed up is the one the session used:

```bash
python3 -m lotto.main session --db /app/data/live.sqlite3 --day YYYY-MM-DD
```

Exit 0 means the regular session is covered (first observation within 10
minutes of 09:30 ET, last within 15 minutes of 16:00, no RTH hole longer than
20 minutes). Exit 2 means it is not a full day. Pass `--allow-partial` to print
the report anyway. The coverage clock is weekday Eastern time, not the exchange
holiday calendar.

## Grading boundary

The stop-or-+40% / +100 card grader is not in this repository. `nightly` writes
`DATA_DIR/nightly/YYYY-MM-DD/report.json` with stock-return labels and sampled
bids. It requires `--day` and refuses an incomplete capture unless
`--allow-partial` is set. The saved report then has `full_day_grade: false`.
The in-process catch-up after the close still runs, and it stamps the same
field. That catch-up is not permission to publish a win rate.

Coverage uses the union of `discovery.at` and `snapshots.at`. Those are the
collector heartbeats. Alert timestamps, decision timestamps, and one
contract's option quotes do not fill a hole and do not reject a quiet session.
Zero alerts with heartbeats from the open through the close is a complete day.
The close is `snapshot.session_end` when the first and last snapshots of that
ET date agree, including a 13:00 early close. Otherwise the close is 16:00 ET.
Saturday and Sunday are `market_closed`, not a missing capture. A weekday with
no heartbeats is ungraded. This repo has no exchange holiday calendar, so a
holiday is not inferred.

Before an external grader runs:

1. Call `session --db <the volume file> --day <the session date>`.
2. Grade only when the exit code is 0 (`grade_as_full_day` true, `missing_capture` false).
3. On exit 2, mark the day partial. Do not fill in the missing open.
4. Pass that same path and date into the grader. Do not open a new empty path
   and do not default to yesterday or to whichever file is newest.

## Rollback

1. Stop the worker.
2. Move the current file aside (`live.sqlite3` to `live.sqlite3.after`). Do not
   delete it.
3. Copy the backup to `live.sqlite3`. If the backup command was used, that file
   is a complete database and the sidecar files are not required.
4. Redeploy the previous image (`7ef6597`) if this build must come off the box.
   That image ignores `schema_meta`, `runtime_starts`, and snapshot fields it
   does not know. It does not delete them.
5. Unset `LOTTO_ALLOW_EMPTY_LIVE_DB` if it was set for a first start.

A rollback does not need a schema downgrade. Leaving version 2 in place is
safe for the previous build.

## Gates that stay in shadow

`LOTTO_FAR_OTM_TIME_MODE` and `LOTTO_DM_ATR_BASIS_MODE` default to 1. Mode 1
writes the flag and the inputs (both ATR values, the basis that actually
blocked, the 0DTE distance, minutes left, quote, and timestamps) onto the
candidate and the decision. It does not change which cards post.

Do not set either mode to 2 from this change:

- Far-OTM mode 2 is a 0DTE rule, `direction * (strike - spot) / ATR / sqrt(minutes_left / 390) >= 0.8`, using the high-low ATR when the snapshot has one. The 0.8 cutoff was selected after looking at every split. The attached research file reports out-of-sample 44.0% to 44.6% and recent 39.1% to 40.2%. This repository does not contain that card harness, so those figures were not recomputed here. The same file's Friday result is 13% to 15%. That does not clear a day like Nov 6.
- ATR mode 2 would block dead-zone and burst-on-flat on the high-low mean. Live blocking today is the true range, because that is what `prior_atr` has always been. The attached comparison says the high-low basis keeps about 80% of held +100 runners and the true-range basis keeps about 76%. Friday, if the blocked names had been released, was reported near 4 wins in 41 cards. Mode 2 waits for a later sample that still keeps at least 80% of held +100s.

`LOTTO_DEAD_ZONE_MODE` and `LOTTO_BURST_FLAT_MODE` are unchanged. Code defaults remain 1. A deployment that already sets them to 2 keeps blocking. `LOTTO_SPENT_MODE` stays 1. There is no new market-direction gate and no loss kill-switch. Unset the two new mode variables to roll the shadow telemetry back; the previous build ignores the extra snapshot fields.

## Exit replay

`python3 -m lotto.main exit-replay --db PATH --day YYYY-MM-DD` replays stored option bids for that date. It is read-only. The default specs are a -35% bid stop against a +40% target and the same stop against a +100% target. `--time-stops` adds minute exits. A quote hole that jumps the stop or the target is `ambiguous` (`ordering_unknown`). The landing print is not treated as stop-before-target. A path that never prints a rule is `censored`. Pass `--allow-partial` to print a partial session anyway; the report still says `full_day_grade: false`.

This harness does not change live stops, and a run of it is not an out-of-sample exit study. No market-direction gate or loss kill-switch was added. Those failed the earlier held-out checks and are not in this build.

## Still outside this repository

- The stop-or-+40% / +100 card grader, including which database file and which session date it opens. `session` is the check it must pass. It is not wired in here.
- The card-level harness behind the attached OOS table and the Friday 4/41 recount.
- The production Railway volume attachment (`RAILWAY_VOLUME_NAME` at `DATA_DIR`). `railway.json` does not declare it.
- Exchange holiday dates. Weekends are handled. A closed weekday is not labeled a holiday.
- Any promotion of far-OTM or high-low ATR from log-only (mode 1) to blocking (mode 2).
