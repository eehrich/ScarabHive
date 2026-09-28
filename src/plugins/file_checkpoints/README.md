# file_checkpoints

Checkpoints for **files**, not only for the conversation (F7, #097). `/undo`
takes a turn out of the conversation and leaves the files the agent wrote in
it as they are. With this plugin the chat can put those back too:

- `/undo files` -- drop the last exchange **and** put back every file it
  changed (`/retry files` likewise);
- `/rewind` -- list the checkpoints of the session (one per turn that changed
  files), `/rewind <n>` -- put the files back as they were before checkpoint
  n, the conversation stays as it is;
- `overwrite` after either -- also for files changed outside the agent since.

Both surfaces: agent-cli's chat and the browser (`POST /chat/undo` with
`"files": true`, `GET /chat/checkpoints`, `POST /chat/rewind`). The model gets
no tool: undoing its own work is the person's call.

**Plugin type:** hooks only (`SchemaBasedPluginHook`)
**Hooks:** `record_before_change` (pre_tool_call), `record_after_change` (post_tool_call)
**Default:** the instance is loaded (`config/plugins.yaml`), both hooks are off
for every agent. The coder harness switches them on (coder and its three
sub-agents, and what inherits from them: gamedev, gamedev_tester).

## Switching it on

Per agent, in its YAML -- **both** hooks, full names:

```yaml
agent_config:
  hooks:
    overrides:
      file_checkpoints.record_before_change: {enabled: true}
      file_checkpoints.record_after_change: {enabled: true}
```

One without the other records nothing and logs an error once per agent: what a
call left is how a rewind tells the agent's change from one made outside it.
`test_plugin_file_checkpoints_config.py` checks the shipped configuration for
exactly that.

## What is recorded

| Tool (server type) | Recorded |
|---|---|
| `file_ops` `manage` create / delete / move / rename, `replace_string_in_file` -- every instance (`coder_fs`, `workspace_file_ops`, ...) | the prior state of each path: content and mode, a link's target, a directory -- or "did not exist". Directories a delete or move takes along are walked; directories a create makes on its way are recorded as new |
| `media_ops` `save` | the target file, as above |
| `terminal` execute, `coding_cli` run_task, `blender`, `godot`, `audio_ops`, `image_compose`, `comfyui`, `sqlite_query`, `stategraph` runs (+ `untracked_tools`) | **not recorded -- counted**: a rewind names them ("Not tracked: coder_shell_execute x3") |
| everything else (reads, search, todo, ...) | nothing |

The paths come from the server's own sandbox (`validate_path`,
`PathSandbox.resolve`), so the path recorded is the path the tool writes. A
path the sandbox refuses is not recorded -- the tool refuses it too.

**Not recorded:** throwaway sessions (`ephemeral-…`: openai_api without
`store`, a stateless `collect_final_result`) and the sub-agents they start --
such a run promises to leave nothing behind.

**One file, one record.** On a case- and normalisation-insensitive file system
(macOS' APFS) `Foo.txt`/`foo.txt` and a composed/decomposed `Müller` are one
file. Paths are recorded under the spelling their directory stores
(`fs.spelled_on_disk`), and grouped by it again when a rewind is planned --
two spellings recorded in one step, before either existed, are one file then.
The roots are spelled the same way, and a file comes back under the name it had
("Foo.txt", even if a later call made it "foo.txt").

Once per turn and path: a file edited twenty times in a turn costs one copy,
and a file nobody touches costs nothing. After the call the post hook records
a digest of what it left; a call that changed nothing (a failed replace) is
dropped from the record again.

**Turns.** A change belongs to the turn that was the conversation's last one
when it was recorded -- the message `/undo` cuts at. Turns are identified by
their head message (role, run id, timestamp), and each recorded turn keeps the
keys of the turns before it. That places every record in the conversation as
it is now: a turn `/undo` dropped without its files stands right behind the
turn before it, so a rewind to that earlier turn undoes it too, while a rewind
of a later turn -- a question asked again -- does not.

**Sub-agents.** A run started under a recorded run records into that run's
session and turn -- provided its agent has the hooks on. "Started under" is
checked twice: its request id extends the run's by a suffix the framework
appends (`_NNN` tool call, `_tsNN` script call, `_sub_`/`_async_` sub-agent),
and its session is the run's own or a sub-session of it (the `parent_session`
link the sub-agent manager writes). A client's own id `chat_2` after `chat` is
a turn of its own. A sub-agent without them records nothing, and the call
that started it is counted like a shell command ("coder_sam_manage_sub_agent
(agent 'x' records no checkpoints)").

**Scripts.** tool_script's calls pass the same hooks and are recorded like the
model's.

## What a rewind does

For every path changed since the checkpoint: the state before the first change
is the target, the state after the last change is what the agent left.

- **Changed outside the agent** since (current state is not what the agent
  left, or it changed between two calls that both touched it -- in two turns
  or in one): refused, **nothing** is touched, the path is named. `overwrite`
  puts it back anyway.
- **Not restorable** (content larger than `max_file_bytes`, a directory beyond
  `max_entries_per_call`, the recording server no longer loaded, the path no
  longer inside its allowed directories, a link in its path, a directory
  holding the person's files): refused as well; with `overwrite` the rest is
  put back and these are named. Those that may pass (all but a content never
  kept) **stay recorded** -- the rewind reports `partial`, and `/undo files`
  keeps the exchange; a later rewind finishes them. Until the reason passes
  (the person clears the directory, the server is loaded again), `/undo files`
  keeps answering `partial` (a 500 with the list in the browser); a plain
  `/undo` drops the exchange and leaves the record for a later `/rewind`.
- Every path is looked at again right before it is written or removed: one
  that changed since the plan (a person at work) is left as it is and named.
- A directory is only removed when everything in it goes with it: a rewind
  **never deletes a path it has no record of**.
- Writes never follow the last component of a path: a file is replaced by a
  rename, a link is recreated as a link, a delete unlinks. The allowed
  directories of every server that recorded a path are checked on its resolved
  parent when the rewind is planned, and again right before each write -- a
  link the rewind itself puts back higher up must not carry a write out.
- A write that fails (permissions) leaves that path recorded -- try again --
  and `/undo files` keeps the exchange.
- No rewind while a run of the session (or a sub-agent it started) is still
  going: it could write while files are put back.

After a rewind the turns it undid are forgotten; a files-only rewind leaves
them in the conversation.

**Numbers.** A checkpoint's number is its record's own sequence, not its place
in the list: forgetting an old turn does not shift the others, so `/rewind 7`
means the same checkpoint before and after. Numbers therefore have gaps.

## Where, and how much

`data/file_checkpoints/<user>/<session>/journal.db` (SQLite: turns, changes,
untracked calls) plus `blobs/<sha256>` (the replaced bytes, each once). Beside
the session directory: with `AGENT_SESSION_STORAGE_PATH` set (an isolated API,
tests) the records go next to those sessions. The user is part of the address
and the journal names its owner: a rewind only ever reads the asking user's
record, and the endpoints check session ownership first.

| Key | Default | |
|---|---|---|
| `max_checkpoints` | 50 | turns kept per session; the oldest is forgotten first |
| `max_file_bytes` | 10 MB | larger files are recorded but not copied |
| `max_call_bytes` | 256 MB | copied content per call |
| `max_session_bytes` | 512 MB | copied content per session; older turns make room, the current one never goes |
| `max_entries_per_call` | 2000 | a bigger directory is recorded as one, not copied |
| `retention_days` | 0 | records untouched this long are deleted -- **off** unless set (destructive) |
| `storage_path` | "" | beside the sessions |

Forgetting old turns only shortens how far back a rewind reaches -- it deletes
copies this plugin made, never a file of the person's. Deleting whole records
by age is the operator's decision, hence off. **Deleting a session**
(`DELETE /api/sessions/{id}`, the person's decision) takes its record along
(`file_rewind.forget_session_files`). The session archive does not: an
archived session can be restored, and its checkpoints with it.

**Cost, measured 2026-09-28** (this Mac, scripted LLM so nothing else takes
time): a turn of 600 calls (200 edits, 200 creates, 200 reads of 4 KB files)
ran 13.14 s without the hooks and 13.93 s with them (medians of three) --
+1.3 ms per call, the record 1.07 MB. The recording itself is ~1.0 ms per written file (snapshot,
SQLite row, blob, digest after). Against real LLM latency (seconds per step)
this disappears.

## Model Experience

### What the model sees

Nothing. No tool, no injected message, no changed result: both hooks return
the call and its result unchanged, and neither blocks (a record that cannot be
written is logged, and the call runs unrecorded).

### Token and cache effect

None -- nothing reaches the prompt or the history.

### Known gaps

- **Shell commands and other tools that write without naming the file** are
  not recorded, only counted. Recording them would need a snapshot of the
  whole tree around every call (Gemini CLI's shadow git repository). Measured
  on this repository (3,252 tracked files): 2.8 s and 55 MB for the first
  snapshot, 30 ms per later one -- but per session, and with `.gitignore`
  honoured it does not see `data/workspace` at all, the default root of
  `file_ops` (ignored by the repository's own `/data/`). Claude Code draws the
  same line (its checkpoints do not track bash changes).
- **A sub-agent without the hooks** records nothing; the call that started it
  (`manage_sub_agent` create/continue, an agent called as a tool) is named in
  the report instead.
- **Stategraph machines** call their tools unhooked; their runs are counted,
  their file changes are not recorded.
- **Slash commands and web buttons** that write files pass no tool hook.
