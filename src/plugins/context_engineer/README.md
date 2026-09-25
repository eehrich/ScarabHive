# Context Engineer Plugin

Advanced context window optimization for long-running agent conversations using layered compaction strategies inspired by LLMLingua, MemGPT, and Anthropic's context management patterns.

## Overview

The Context Engineer plugin provides intelligent context management to prevent token limits from being exceeded during long conversations. It implements a multi-layered approach:

1. **Tool Result Store** - Compact references to tool outputs and attached files
2. **Media Store** - Inline audio/images written to disk before eviction
3. **Core Memory** - Always-present important facts (MemGPT pattern)
4. **Archival Memory** - Searchable conversation history with FTS5
5. **Layered Compaction** - Progressive compression strategy

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Context Engineer Plugin                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐     │
│  │   Core Memory   │  │  Tool Result    │  │  Media Store    │     │
│  │  (always in     │  │    Store        │  │  (files on      │     │
│  │   context)      │  │  (SQL refs)     │  │   disk)         │     │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘     │
│           │                    │                    │               │
│           └────────────────────┼────────────────────┘               │
│                                │                                    │
│                    ┌───────────▼───────────┐                        │
│                    │  Layered Compaction   │                        │
│                    │  P:  Message count    │                        │
│                    │  L1: Reversible       │                        │
│                    │  L2: Semi-reversible  │                        │
│                    │  L3: Last resort      │                        │
│                    └───────────┬───────────┘                        │
│                                │                                    │
│                    ┌───────────▼───────────┐                        │
│                    │   Archival Memory     │                        │
│                    │  (FTS5 + ChromaDB)    │                        │
│                    └───────────────────────┘                        │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Hook: pre_llm_call  │  Tools: list, read, store_fact, compact     │
│                      │                                              │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

### Core Memory (`core_memory.py`)

Compact storage for important facts that should always be present in the system prompt. Inspired by MemGPT's tiered memory architecture.

```python
from plugins.context_engineer.core_memory import CoreMemory

memory = CoreMemory(storage_path=Path("memory.json"), max_tokens=2000)

# Add facts with optional importance scores
memory.add_fact("User prefers Python over JavaScript", category="preferences", importance=0.8)
memory.add_fact("Project uses FastAPI framework", category="technical")

# Get formatted section for system prompt
section = memory.to_system_prompt_section()
# Returns: "<core_memory>\n- User prefers Python over JavaScript\n- ..."
```

### Tool Result Store (`tool_result_store.py`)

Stores full tool outputs externally and replaces them with compact references in the conversation. Outputs can be retrieved by ID or content hash.

```python
from plugins.context_engineer.tool_result_store import ToolResultStore

store = ToolResultStore(Path("tools.db"))

# Store and get reference
ref = store.store_and_reference(
    tool_call_id="call_abc123",
    tool_name="read_file",
    content="... large file contents ..."
)
# Returns: "[Tool:read_file ref:call_abc1 hash:f7c3bc1d]"

# Retrieve later
entry = store.retrieve_by_id("call_abc123")
print(entry.content)  # Full content
```

> **Entfernt:** Die $VAR-Ersetzung (`variable_manager.py`) gab es bis 2026-08.
> Gemessen ueber 1000 produktive Kompaktionen: 9 angelegte Variablen gegen 3730
> ausgelagerte Tool-Ergebnisse — bei 6 von 1000 Ereignissen ueberhaupt aktiv.
> Sie kostete dabei zweimal Cache: das Umschreiben alter Assistant-Nachrichten
> brach den Praefix ab dieser Stelle, und ihre System-Prompt-Sektion listete
> jede Variable namentlich, aenderte sich also bei jeder neuen und entwertete
> den Cache fuer die ganze Konversation dahinter. Angehaengte Textdateien
> liegen jetzt im Tool-Result-Store: gleiche Form (Inhalt, Ref, Zusammenfassung),
> und `list`/`read` bedienen ihn ohnehin schon.
### Archival Memory (`archival_memory.py`)

Searchable archive of conversation history using SQLite FTS5 for text search and optional VectorStore for semantic search (supports ChromaDB and sqlite-vec backends). With semantic search on, `search()` asks BOTH and fuses the two rankings (reciprocal rank fusion): a message both agree on ranks first, and one only the text index knows -- not embedded yet, or never, after a refused or cancelled batch -- still gets the place its text rank earns. The text half demands every word of the query there: fusion weighs by rank alone, and a row that matched only a common word would otherwise take a slot at the weight of a real hit. When the vector index has nothing for the session at all, the search is the broad text search (any word), exactly as without semantic search. Asked alone, the vector index answered over the part it held and never said which part that was.

```python
from pathlib import Path

from plugins.context_engineer.archival_memory import ArchivalMemory

archive = ArchivalMemory(Path("archive.db"), session_id="session_123")
messages = [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]

# Archive a message, or many in one transaction
entry_id = archive.store({"role": "assistant",
                          "content": "The Python code processes data in batches..."})
ids = archive.store_many(messages)

# Search: text index, fused with the vector index when semantic search is on
results = archive.search("batch processing", limit=5)
```

### Layered Compaction (`compaction.py`)

Progressive compression strategy that applies increasingly aggressive techniques based on token budget.

**Pre-Layer T (on arrival):**
- A new tool result larger than `tool_result_max_window_share` (default 0.25) of the model's context window is stored right away, below every threshold and hysteresis. The agent reads it back with `read` (paged, or `find=` for the matching parts).
- The bound is a share of the window, not a token count: a 1M model keeps a 100k chapter inline (the read tool pages 5000 characters at a time). Lower the share per agent for a tighter cap.
- **A summary instead of a pointer** (`tool_result_summary_from`, tokens, 0 = off; `tool_result_summary_profile`, default `summarizer`, and an empty one switches it off too): a result from that size on is stored AND a cheap model writes what it says into the placeholder the conversation keeps. This is for hand-overs — the expensive agent that was handed a sub-agent's answer pays for every token of it, and a bare pointer costs it a read call before it can act at all. The full text stays in the store; `read` returns it whole. The threshold is a token count, not a share: what the next agent pays does not shrink because its window is large.
  - **Prose only.** A structured result (JSON) is neither summarized nor taken out of the conversation for this — a pointer nobody summarizes would cost the next agent a read call instead of saving one. A wrapper around prose is summarized by its prose, nested too (`result`, `content`, `text`, `output`, `answer`, `data`, three levels deep) — that is the shape the sub-agent manager, coding_cli and n8n hand over. The prose must be at least 80 % of the result: below that the other fields carry their own facts (a build result's `status`, `error` and `exit` next to its log), and a summary of the log alone would drop them.
  - **Per tool, if you name them** (`tool_result_summary_tools`, fnmatch patterns over the tool name, which carries the instance prefix: `sub_agent_manager_*`). Empty, the default, means every tool -- right for an agent that only takes hand-overs. An agent that also reads files through the same hook names the hand-over tools here, or a long file arrives as a summary it has to read back: a turn spent instead of saved. Matching is case-sensitive on every platform. A result whose message carries no tool name (a session restored mid tool-turn) stays whole as soon as patterns are set at all, `*` included -- no pattern an operator can write names it. With no patterns it is summarized like any other. A value that is not a list of patterns is dropped with a warning to a pattern that matches nothing, not to the empty list: empty means every tool, so a typo would otherwise turn this key into the most permissive setting there is.
  - **On arrival only.** Layer 1 stores results too, the whole history at once; a model call per result there would spend the hook's budget and the compaction would be dropped with it. What Layer 1 stores keeps the cheap preview.
  - **Bounded on both sides.** At most 60k characters of the result are shown to the model (the rest is only in the store, and the summary says so), at most 1200 characters come back (cut with `…`), and an answer longer than half the original counts as an echo, not a summary — measured on what the model wrote, before the cap.
  - **One time budget per round**, not per call: 20 s for all summaries of the arrivals together, and under a second left no call is started. A round of parallel sub-agent results would otherwise add up past the hook's own budget, and the hook is dropped whole — with the storing every other result was due. A failed, timed out or cancelled call keeps the pointer; the turn never fails over a summary.
  - **The profile is a name, not a model.** `summarizer` is a profile in `config/llm.yaml`; which model condenses text is configured there, once, for every plugin that summarizes. Nothing in this plugin names a model.
  - **It leaves the process.** Every summarized result goes to that profile's provider — file contents included, since `read_file` answers are prose too. Pick a profile you trust with what the agent reads, and switch the feature on per agent (`hooks.overrides`), not plugin-wide.
- Only the current round (after the last assistant message the model wrote) is touched: messages a request already carried and their reasoning artifacts stay as they were sent. The one block of our own is the restoration section, appended as a `developer` turn at the END (it used to sit behind the system prompt, where rebuilding it invalidated the cached prefix behind it); it explains how to read a stored result: it gains its "Tool Results" section when a session stores its first one, and for an agent whose calls normally skip the hook (no always-on media compaction) it is inserted whenever the hook runs — the same as on a Layer 1 run.

**Pre-Layer P (message count, off by default):**
- Past `max_messages`, the oldest messages are archived and removed until `max_messages_prune_to` remain (0 = half the limit). No LLM call; the agent finds them again through the retrieval tools. With `enable_semantic_search` on, a prune larger than 200 messages writes its rows inside the request and embeds them in a background task, in chunks of 200, one batch at a time per loop: they are listed, readable by ref and found by their words at once; search by meaning reaches them a little later. The old answer was to skip the index there, and a half-indexed archive answers every similarity search over half of itself without saying so. Three things end an embedding early, and each says so in the log: a vector store that refuses a chunk, a session evicted under the task, and a one-shot CLI run whose loop closes while it is still going (the API's loop lives as long as the process). Without semantic search no embedding happens at any size and no task is started. What was not embedded is still readable by its ref and found by a filter whose every word it contains; not by meaning.
- It is a hysteresis: each prune breaks the prompt cache, the next one comes about `max_messages - max_messages_prune_to` messages later (`min_tokens_between_compactions` can hold it longer). `200` / `100` breaks at most once per 100 messages; `max_messages_prune_to` equal to the limit prunes whenever the list is over it.
- The task (first user message), the last user message, system messages and the round the model has not seen yet stay. Among the oldest messages placeholders go before real content; the choice stops before the newer half of what stays (a tool-call unit at that edge still leaves whole).

**Layer 1 (Reversible):**
- Store tool outputs with compact references
- **Media Deduplication** - Auto-detect duplicate media by file hash, compact older duplicates (keep newest)

**Layer 2 (Semi-Reversible):**
- Archive old messages with summaries
- Truncate very old tool results

**Layer 3 (Irreversible):**
- Drop old messages entirely
- Compress remaining summaries

```python
from plugins.context_engineer.compaction import LayeredCompactionStrategy, CompactionConfig

config = CompactionConfig(
    layer1_threshold=80000,   # Start reversible compaction
    layer2_threshold=100000,  # Start semi-reversible compaction
    layer3_threshold=120000,  # Start irreversible compaction
    target_tokens=60000       # Target after compaction
)

strategy = LayeredCompactionStrategy(
    tool_store, core_memory, archival_memory, config
)

result = strategy.compact(messages, current_tokens=95000)
# result.modified_messages contains compacted conversation
# result.tokens_saved shows reduction
```

## Tools

The plugin exposes these tools to the agent:

| Tool | Description |
|------|-------------|
| `list` | Browse what is stored, or filter it — refs, summaries, excerpts; no bodies |
| `read` | Read ONE ref, always bounded, says how to continue |
| `store_fact` | Add important fact to core memory |
| `compact` | Reversible compaction now, without token threshold |

`recall`, `get_variable`, `get_tool_result`, `stats` and `restore_multimodal`
were removed in 2026-08. The last four were already unreachable: they were not
declared in `schema.yaml`, and the schema is what the dispatcher routes on.
`recall` guessed the store from a free-text query and is fully covered by
`list` + `read` — including restoring media, via `read(ref="…/clip.wav")`.

### list / read

Browsing and finding are ONE verb: they answer one question and hand back one
shape. `list` alone pages through what is stored; `list(filter=…)` narrows the
same rows to what matches, each carrying the matching excerpt. `read` is the
only thing that returns a whole item, and only in bounded pieces.

Two tools rather than three because a separate `search` duplicated the row
format for one extra parameter — and rather than one, because `read` needs `ref`
to be a *required* field. Folding it in would leave `operation` as the only
thing the schema can demand, and a `read` without a ref would stop being an
API-level rejection and become a runtime error.

**Neither returns bodies.** Content comes only from an explicit `read`. The tool
these replaced returned a stored tool result whole: measured in production at
134k characters in a single call, which undid the compaction that had put it
away and usually delivered far more than the agent needed. A filter's excerpt
(~460 chars around the hit) is frequently the whole answer, so the ref is an
option rather than a second required round-trip.

**Browsing and filtering differ in one respect, and it is visible.** Browsing
pages through ONE store in a defined order, so `section` defaults to the
conversation and the reply carries `next_offset`. Filtering has no order across
stores, so it searches all of them and offers no `next_offset` — promising a
stable next page that does not exist would be the same silent untruth as a
truncated answer with no way to continue. The reply always echoes the `section`
it used.

**Every read is bounded and says how to continue.** `next_offset` in the reply is
the difference between a truncated answer and a dead end. For a large item,
`find=` returns only the parts that mention something instead of paging through
all of it.

**References are declared, not guessed.** `recall` inferred from the shape of a
free-text query which of five stores was meant, and was documented misrouting
agents who wrote `$TR_…`. Every store owns a prefix, so dispatch is a lookup:

| Ref | Store |
|---|---|
| `arch_…` | an archived message |
| `TR_…`, `call_…` | a stored tool result |
| `…/file.png` | media to restore |

A ref that is not one of these is an error naming the valid shapes — never a
silent fallback into a keyword search.

The addresses need not be looked up at all in the common case: compaction leaves
`{"type":"archived_ref","ref_id":"arch_…","summary":"…"}` exactly where the
message stood, so the shortest path back is to read the ref that is already in
view.

```yaml
# What is in my archived history?
list: {}                                     # -> refs + summaries + next_offset
list: {offset: 20}                           # next page
list: {section: tool_results}                # stored tool outputs

# Find it by keyword (messages, tool outputs and facts)
list: {filter: "database optimization"}      # -> refs + matching excerpts

# Read one of them
read: {ref: "TR_abc123"}                     # first 2000 chars + next_offset
read: {ref: "TR_abc123", offset: 2000}       # the next piece
read: {ref: "TR_abc123", find: "Kapitel 3"}  # only the matching parts
```

### Other tools

```yaml
# Store important fact
store_fact:
  fact: "User prefers async code patterns"
  category: "preferences"
  importance: 0.9

# Manual compaction (also /compact at the chat prompt)
compact: {}
```

A manual compaction runs Layer 1 whatever the token count — only Layer 1,
because it is the reversible one: tool results become references, files move
to the store, media goes to disk. What it takes is still decided by its own
rules (`tool_result_keep_last`, `tool_result_min_size`). Layers 2 and 3 take
messages out of the conversation and keep their thresholds, manual or not.

## Hook Integration

The plugin registers a `pre_llm_call` hook that:

1. Checks current token usage against thresholds
2. Applies layered compaction if needed
3. Appends the restoration block as a `developer` turn at the end -- only when it differs from the block written last
4. Returns optimized messages to LLM

## Configuration

Configure in `config/plugins.yaml`:

```yaml
context_engineer:
  enabled: true
  config:
    # Token thresholds
    layer1_threshold: 80000
    layer2_threshold: 100000
    layer3_threshold: 120000
    target_tokens: 60000
    
    # Core memory
    core_memory_max_tokens: 2000
    
    # Tool results
    tool_result_min_size: 500
    tool_result_keep_last: 3
    
    
    # Archival
    archive_after_turns: 20
    semantic_search: true   # Enable VectorStore semantic search (auto-detects ChromaDB or sqlite-vec)
    
    # Media deduplication (new)
    deduplicate_media: true   # Auto-compact older duplicate media files by hash
    
    # Event-based media compaction (new)
    compact_media_after_user_message: false   # Compact all media when new user message arrives
    compact_media_after_final_response: false # Compact all media when agent sends final response
    
    # Memory Management
    session_ttl_seconds: 7200          # Session cleanup TTL (default: 2 hours)
    max_tracked_sessions: 100          # Max sessions before LRU eviction
    session_data_ttl_days: 14          # Delete a session's on-disk data (archive, tool results, media) and its vectors after N idle days; 0 (default) = off
```

## The panel

**Context Engineer** in the panel launcher (category *context*), or from a session's info button
(`/plugins/context_engineer/?session_id=<id>`: the panel then keeps to that session). It shows either the session open
in the chat (*This session*) or every session (*All sessions*):

- **Figures**: compactions, tokens saved, average reduction and media compacted (evicted by the media window,
  duplicates, after events). For a session also what its stores hold on disk: stored tool results, archived messages and
  core memory facts, each with its tokens.
- **Core memory** (a session only): the facts as `core_memory.json` holds them, by category and importance.
- **Compactions**: the newest 100, newest first — agent, (for all sessions) session, tokens before and after, saved,
  reduction, tool results stored, media and the layers applied (hover a badge for what the layer does).

With no session open it says so and asks for nothing; *All sessions* still works. The panel refreshes every 10 s while
visible (`<pk-refresh>`). The compaction history is the hook's in-memory list (the last 1000 are kept in
`data/context_engineer/history.json` across restarts).

### Endpoints

| Method and path | Answer |
|---|---|
| `GET /plugins/context_engineer/` | the panel |
| `GET /plugins/context_engineer/history?session_id=&limit=100` | `{events, stats}`: the newest `limit` (1–1000) compactions of the session (all without `session_id`), newest first as the hook records them; `stats` = `events`, `tokens_saved`, `average_reduction` (percent, `null` without events), `media_always_compacted`, `media_deduplicated`, `media_compacted_after_event` over every event asked for |
| `GET /plugins/context_engineer/session?session_id=` | `{tool_results: {count, tokens}, archived: {count, tokens}, core_memory: {facts: [{content, category, importance}], tokens, max_tokens}}`; `archived` counts the messages tagged with the session (what `list` reaches); a session without a directory holds nothing; a store that cannot be read → 503 with the reason |

Who sees what (`agent_system/auth/session_access.py`, the rule of the usage tracker too): a user her own sessions --
another user's answers as a session without compactions and without stores. Every session at once is an admin's
(403 otherwise); with authentication off, everything is shown.

`session_id` must match `^[A-Za-z0-9_-]+$` (422 otherwise): it names the session's directory. The endpoints only read:
the stores are opened read-only and the core memory file is parsed, not loaded — nothing creates a session's files or
registers the session with the hook.

### Tests

`tests/test_plugin_context_engineer_panel.py` drives the panel in headless Chromium (`tests/panel_tests.html`) against the
real plugin router and real session stores under pytest's `tmp_path`; the real history file is neither read nor written.

## Media Management

### Media Deduplication

When the same image, audio, or video file appears multiple times in a conversation, the plugin automatically detects duplicates using file hashes and compacts older occurrences. Only the **newest** version is preserved in full.

```yaml
# Enable/disable (enabled by default)
deduplicate_media: true
```

**How it works:**
1. Media items are hashed by file path (or inline data hash as fallback)
2. Duplicates are identified across all messages
3. Older duplicates are replaced with text placeholders
4. The newest occurrence is preserved for the LLM

### Event-Based Media Compaction

Optionally compact **all** media when certain events occur:

```yaml
# Compact all older media when a new user message arrives
compact_media_after_user_message: false

# Compact all older media when the agent sends a final response
compact_media_after_final_response: false
```

This is useful for:
- Conversations with many images where only the latest matters
- Saving tokens after the agent has processed media
- Multi-turn conversations where early media is no longer relevant

**Note:** Media in the most recent message is always preserved.

## Integration with Other Plugins

### context_summarizer
The context_engineer can work alongside context_summarizer. Use context_engineer for structured storage and retrieval, and context_summarizer for LLM-generated conversation summaries.

## Storage Locations

Data is stored under `data/context_engineer/{session_id}/`:

```
data/context_engineer/
└── {session_id}/
    ├── core_memory.json      # Important facts
    │                         # (variables.json may still exist from before
    │                          2026-08; nothing reads it and it can be deleted)
    ├── tool_results.db       # SQLite tool outputs
    └── archive.db            # SQLite archived messages + FTS
```

## Token Estimation

The plugin uses `estimate_content_tokens()` from `agent_system.llm.token_utils`, a character-based estimate (3.3 characters per token, 2.85 for JSON) fitted to real prompt tokens of the production models. The hook compares it with the provider's count of the previous call and uses the larger of the two.

## Best Practices

1. **Add facts proactively** - Use `store_fact` to preserve important information before it's compacted
2. **Reference stored content** - Leave a ref where the content was, so the LLM can fetch it on demand
3. **Monitor stats** - Use the web panel to track compactions and what the stores hold
4. **Tune thresholds** - Adjust compaction thresholds based on your model's context window
5. **Use semantic search** - Enable ChromaDB for better archival retrieval in long conversations

## Error Handling

The plugin handles errors gracefully:
- If archival storage fails, messages are kept in context
- If compaction fails, original messages are preserved
- Missing tool results return helpful error messages naming where to find valid refs

## Future Enhancements

- [ ] LLM-based summarization for archived messages
- [ ] Automatic importance scoring for facts
- [ ] Cross-session memory sharing
- [ ] Token budget prediction based on conversation patterns
