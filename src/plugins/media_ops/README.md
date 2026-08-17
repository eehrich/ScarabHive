# Media Ops Plugin

Loads images and audio from disk **into the agent's content**, and writes
media that is already in the context back **out to disk**. Three tools:

| Tool | Purpose |
|---|---|
| `<instance>_load` | Read a file from disk and hand it to the model as real multimodal content |
| `<instance>_list_context` | Enumerate the media currently in the session, with the ids `save` needs |
| `<instance>_save` | Write one of those back to disk |

`load` works through `_multimodal_content` — the key `tool_execution.py` pops
and converts into `MultimodalToolContent`, so the model receives an actual
image or audio block, not a path string.

`list_context` exists because the model cannot address what it cannot name:
it sees the media, but not a stable id per item. `save` needs that id.

## Configuration

As shipped in `config/plugins.yaml`:

```yaml
media_ops:
  type: media_ops
  enabled: true
  allowed_directories:
    - data          # relative paths resolve against the project root
  max_file_size_mb: 20
```

One root is enough because everything plugin-produced — ComfyUI output,
`audio_ops` storage, covers, the `context_engineer` media store — lives under
`data/`. Keep the list minimal: each entry is readable AND writable by any
agent holding these tools.

`allowed_directories` is the whole security model, in **both** directions —
`load` may not read outside it and `save` may not write outside it. Paths are
resolved (`Path.resolve()`) before the containment check, so `..` chains and
symlink escapes are caught. Verified against 25 payloads including `\\?\`
prefixes, UNC paths, alternate data streams and real directory junctions —
including the case where an allowed directory is itself a junction.

## Model Experience

### What the model sees

`load` on success — plus the actual image/audio block:

```json
{"status": "success", "path": "...", "type": "image",
 "mime_type": "image/png", "size_mb": 1.42}
```

`list_context` returns one entry per media item with its `id`, origin and
message index. `save` returns the written path.

On a path outside the sandbox, both `load` and `save` return this verbatim
(`error_type: "PermissionError"`):

```text
Path is outside the allowed media directories: <path>. Allowed: <roots>
```

When `save` targets an item the plugin cannot reconstruct:

```text
Media <id> cannot be saved: <reason>
```

Every failure is a normal `{"status": "error", ...}` result — the tool never
raises into the agent loop.

### Token and cache effect

**Append-only.** Results are appended at the end of the conversation; the
plugin never rewrites an existing message, the system prompt, or the tool
list. No prompt-cache invalidation from this plugin.

Cost is dominated by `load`: an image enters the context as image tokens, not
as the ~80 tokens of the JSON envelope — a 1-megapixel image costs roughly a
thousand. `list_context` is proportional to the media count (~30 tokens per
entry). `save` is negligible.

Note that `context_engineer` may later evict loaded media from the context.
That is the intended division: this plugin puts media in, the compaction
plugin decides how long it stays, and `save` still works as long as the item
is listed.

### Known gaps

- **No quota.** A caller can fill `allowed_directories`; `max_file_size_mb`
  bounds a single file, not the total. The boundary is who gets the tool.
- **`save` can only write back what was loaded** — the agent cannot generate
  an image, so there is nothing else to persist. If image generation ever
  produces context media, this assumption needs re-checking.
- **`list_context` reads the live session.** Without session context it
  returns `SessionContextMissing` rather than an empty list, so a caller can
  tell "no media" apart from "cannot see the session".
- **Symlinks are resolved, then contained.** A symlink pointing INTO an
  allowed directory is followed. Deliberate: the alternative rejects ordinary
  working setups.

## Tests

`src/plugins/media_ops/tests/` — 25 tests, all verified by mutation
(production code broken, test must go red).
