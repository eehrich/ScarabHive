# Session Archive

Old conversations move out of `data/sessions/` into ZIP archives — whole
trees at once, and they come back the same way.

## Why

`data/sessions/` grew without bound. A large run leaves behind one root
session and several hundred sub-agent sessions, and nothing ever cleaned them
up. Measured on 20.09.2026 on this machine:

| | Files | Size | older than 30 days |
|---|---|---|---|
| Sub-agent sessions | 59,970 | 5.10 GB | 59,960 |
| Root sessions | 227 | 0.05 GB | 202 |
| Sub-indexes (`.subs.*`) | 1,304 | 0.05 GB | — |

Everyone who walks the store pays for this: a directory listing, the
`iterdir` in `delete_session`, an index rebuild. The most expensive case was
measured at **7 min 31 s** — that is how long `create_session` hung on the
first sub-agent of a request, because a missing sub-index was read as "index
lost" and all 60k files were read (fixed, see `SessionIndex.update_entry`
in `services/session_index.py`). As long as the directory stays this large,
every such spot remains a trap.

## What moves

**The unit is a tree**: a root session with all sub-agent sessions beneath it,
transitively. Half a conversation is worth nothing — an archived parent with
orphaned children would be worse than not cleaning up at all.

A tree moves when **every** session in it is older than `retention_days` and
**none** of them is running. A single fresh session holds the whole tree back.

The age is the `updated_at` value from the index row, falling back to the
file's mtime. Both are cheap to read; no session file is opened for this.

A session that **no index knows** (index drift — 7 of 60,196 on this machine)
becomes its own root and is therefore still picked up. So is a child whose
parent file is missing.

## Where it lives

```
data/session_archive/
  <user>/
    index.json                  one row per archived tree
    index.json.lock             lock for every change to index.json
    .sweep.lock                 lock for a whole sweep
    2026-08/<root_id>.zip       the tree as a ZIP
```

The code is split the same way: `services/session_archive_store.py`
(`ArchiveStore`) is this layout — the paths, the manifest and its lock,
writing, verifying and reading a ZIP — and `services/session_archive.py`
(`SessionArchive`) is the policy over it: which trees go and when, the guards,
the order of a sweep, restore and forget.

**Two processes, one archive.** The API archives on a timer, the CLI on
demand — both write the same `<root>.zip.tmp` and register in the same
`index.json`. That is why both locks are **operating-system locks on files**
(`filelock`), not flags in the memory of one process:

* `.sweep.lock` is held by a sweep for its whole duration. A second one is
  **rejected, not queued** (409 in the panel, one line in the CLI) — whoever
  says "now" does not want a report that speaks about someone else's work
  minutes later. If a sweep dies, the operating system releases the lock with
  the process; a flag in a file would afterwards say "running" forever. A
  **dry run** does not take it: it writes nothing, and holding it would have
  rejected the timer's real sweep for the minutes a large user takes. It does
  not clean up old `.tmp` files either.
* `index.json.lock` is taken for **one** change (register, restore, delete),
  never across an `await`. Without it, a read-modify-write loses what the other
  process registered in between — and a ZIP that nothing points to is not
  healed by any later sweep, because its sessions are already deleted. Anyone
  who waits longer than 20 s gets an error instead of a hang, and the
  **sweep ends there**: every further tree would wait the same time on the same
  lock.

Both tests for this hold the lock from a **real second process**
(`test_a_sweep_in_ANOTHER_process_is_refused`,
`test_an_index_held_by_another_process_is_an_answer_not_a_hang`). Measured in
passing: on Windows a killed process releases its lock only about 0.2 s after
`wait()`.

**One ZIP per tree**, not per month: the tree is the unit that is archived,
restored and deleted, so it is also the unit that is one file. That keeps a
restore down to a single `open`.

**An existing archive is never replaced, only extended.** Whether to extend or
write anew is decided by the **file**, not the manifest — a lost or unreadable
`index.json` would otherwise lead to overwritten archives, and that is the
only failure here that cannot be undone. A restore takes its ZIP with it; a
restored tree therefore starts a fresh one.

Every ZIP carries its own `_manifest.json`. If `index.json` is lost, the
archives are still readable and the index can be rebuilt from them.

The archive lives **next to** `data/sessions/`, not inside it: the
`SessionManager` treats every subdirectory of `data/sessions/` as a user
directory.

## The order

Always: **write the archive → read it back and verify → only then delete the
live files.** A crash in between costs a duplicate, never a conversation. A ZIP
that cannot be reopened or is missing members is discarded — the `.tmp` is not
left lying around.

The manifest entry is written **before** the first file is deleted: from then
on the archive is the only copy, and a finished archive that no index knows
would be overwritten by the next sweep.

Deletion goes **bottom-up**, the root last. If a deletion fails (quite real on
Windows: the file is open in another process), the sweep continues with the
rest and reports the stragglers. The **next** sweep extends the same archive
instead of replacing it — replacing would throw out the siblings that exist
only there now.

Deletion happens exclusively through `SessionManager.delete_session(...,
create_backup=False)`. That keeps index partitions, cache and the tombstones
(`_deleted`) consistent — the archive holds no invariant of its own about the
live store.

## Who is protected

Two guards, because neither sees everything alone:

* **Running jobs** — the `BackgroundJobManager` of the API process
  (`active_sessions()`). If this information is unavailable, **nothing at all**
  is archived: "I don't know which are running" must not be read as "none is
  running". This question is asked **anew for each tree**: a first sweep over
  the backlog runs for about 18 minutes, and a conversation that is resumed in
  minute 12 must not be archived because it was quiet in minute 0.
* **Session presence** — the `<sid>.lock` files. This is the guard that works
  **across processes**, and the only one a CLI process has. It is read once per
  user (a `scandir` over 60k entries is not repeated 200 times). Its failure
  does **not** stop the sweep — unlike the above, and on purpose:
  `session_presence.enabled: false` is a supported setting, and a sweep that
  refuses without presence would refuse on every such machine. If **both**
  guards are gone (CLI without presence), the command line says so before the
  run.

  ⚠️ The lock files are read **directly**, not through
  `SessionPresence.list_for_user`: that method deliberately omits sub-agent
  sessions ("they belong to the run that started them") — and those are 59,970
  of 60,196. A guard built on this method would have overlooked almost
  everything it exists for. `get()` does not filter.

## Restoring

`restore(user, root_id)` writes every session back to its place — `updated_at`
and message count **unchanged**. A conversation comes back as what it was; if
the restore stamped it as "today", it would be safe from the next sweep for a
month without anyone having touched it.

For this there is `SessionManager.reinstate_session()` — the counterpart to
`delete_session`: it writes unchanged and adds the index row back. It does
**not** lift the tombstone from `_deleted`; it does not have to, because
`is_deleted` discards the entry on its own as soon as the file is back.
Lifting it beforehand would open exactly the hole the tombstone closes.

It **refuses** if the session is live again: two sessions under one ID are
corruption, not a duplicate. After a successful restore the ZIP disappears.

Three more things a restore catches:

* **A restore that was aborted halfway can be resumed.** A session that is live
  again as *exactly the archived copy* (same `updated_at`) is skipped instead of
  reported as a conflict — otherwise a half-restored tree would be blocked
  forever. A session that is live under the same ID as something **different**
  remains a rejection.
* **An archive with a foreign `user_id` is rejected**, because
  `reinstate_session` writes to the `user_id` *in the document*.
* **The path from the manifest is checked.** `index.json` is a file on disk,
  not a trusted input — and `forget` deletes whatever is written there.

`forget(user, root_id)` deletes an archive permanently. Afterwards there is no
copy left.

## Usage

**Panel** "Session Archive" (category *session*, plugin `session_archive`):
list, *Restore*, *Delete*, *Archive now*. Every endpoint answers only about the
archive of the **requesting** user; there is no parameter for other users.

The list is sortable by every column (most recently archived first until you
choose otherwise; the choice survives the refresh tick and a reload). Four
columns sort by the value **behind** the cell, not by what is displayed: the
title without the ID beneath it, the size in bytes instead of rounded to
`0.0 MB`, and both dates by the full timestamp — a sweep puts down a whole
batch within the same minute, and they all show the same day.

**CLI**:

```bash
agent-cli run --session-user admin --list-archived
agent-cli run --session-user admin --archive-sessions --dry-run
agent-cli run --session-user admin --archive-sessions 90
agent-cli run --session-user admin --restore-session <root_id>
```

**Automatic**: the sweep in the API process, `first_sweep_delay_seconds` after
startup and then every `sweep_interval_hours`.

## Configuration (`config/config.yaml`)

```yaml
session_archive: # type SessionArchiveConfig
  enabled: true
  retention_days: 30            # a tree moves when EVERY session is older
  sweep_interval_hours: 24.0
  first_sweep_delay_seconds: 300.0
  max_trees_per_sweep: 0        # 0 = no cap: one sweep takes everything
  # archive_path: null          # default: data/session_archive
```

Changes require a **restart** — the service is built at startup.

## What it does not do

* **The `.backup_*.json` files** from `delete_session` are not cleaned up. That
  is a separate retention question (3 files on this machine so far).
* **Heal the main index.** If `index.json` is missing entirely, the
  `SessionManager` still rebuilds it from a full scan — that is the honest
  repair path for a real loss, and it never runs in normal operation.
* **Clean up across users.** Every user has their own archive.

## Measurement and tests

```bash
pytest tests/session/test_session_archive.py -q      # the service
pytest src/plugins/session_archive/tests -q          # the panel
```

Panel in the browser (Chromium, runs on its own):

```bash
pytest src/plugins/session_archive/tests/test_plugin_session_archive_panel.py -q
```

Dry run against the real store (changes nothing):

```bash
agent-cli run --session-user admin --archive-sessions --dry-run
```

Measured on 20.09.2026:

* The forest walk over 60,196 files takes about **one second** (`scandir`
  plus 1,304 index files, no session file opened). The dry run reported 200
  trees with 58,949 sessions; 25 trees were too young.
* A tree with 296 sessions and 26 MB: **5.4 s** to archive, **3.8 s** to
  restore. The backlog of 59,960 sessions is therefore about **18 minutes** of
  background work — one-off; after that a daily sweep takes seconds.
* **`max_trees_per_sweep` defaults to 0, i.e. no cap** — a sweep takes
  everything that is old enough. That was the assignment ("everything older
  than X"), and a sweep that stops at 200 turns a cleanup into a trickle: the
  rest lies there until the next day. There is nothing that needs limiting
  here anyway — writing the ZIPs runs through `asyncio.to_thread` regardless, so
  it does not block the event loop; what remains is contention for the manager
  lock during deletion, and that is the price of the work, not a reason to
  leave it half done.
* Anyone who wants to cap a sweep anyway sets a positive value. A capped sweep
  then states in the log, CLI and panel **how many** are still waiting
  (`remaining`) — "Archived 200" otherwise looks like done, especially in the
  panel, whose list updates underneath. A dry run is never capped: a report
  that stops counting at 200 would read like "there is nothing more".
* `skipped_young` counts **all** too-young trees of the user, not those a sweep
  happened to pass before the cap stopped it. Before, the number grew with
  every sweep although the set did not change (measured on 20.09.2026 on
  `cli_user`: 328, 667, 970).
