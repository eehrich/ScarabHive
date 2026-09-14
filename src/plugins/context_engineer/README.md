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

Searchable archive of conversation history using SQLite FTS5 for text search and optional VectorStore for semantic search (supports ChromaDB and sqlite-vec backends).

```python
from plugins.context_engineer.archival_memory import ArchivalMemory

archive = ArchivalMemory(Path("archive.db"), session_id="session_123")

# Archive a message
archive.store_message(
    role="assistant",
    content="The Python code processes data in batches...",
    turn_number=42
)

# Search text
results = archive.search_text("batch processing", limit=5)

# Semantic search (uses VectorStore - ChromaDB or sqlite-vec)
results = archive.search_semantic("how to handle large datasets", limit=5)
```

### Layered Compaction (`compaction.py`)

Progressive compression strategy that applies increasingly aggressive techniques based on token budget.

**Pre-Layer T (on arrival):**
- A new tool result larger than `tool_result_max_window_share` (default 0.25) of the model's context window is stored right away, below every threshold and hysteresis. The agent reads it back with `read` (paged, or `find=` for the matching parts).
- The bound is a share of the window, not a token count: a 1M model keeps a 100k chapter inline (the read tool pages 5000 characters at a time). Lower the share per agent for a tighter cap.
- Only the current round (after the last assistant message the model wrote) is touched: messages a request already carried and their reasoning artifacts stay as they were sent. The one front change is the restoration block behind the system prompt, which explains how to read a stored result: it gains its "Tool Results" section when a session stores its first one, and for an agent whose calls normally skip the hook (no always-on media compaction) it is inserted whenever the hook runs — the same as on a Layer 1 run.

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

## MCP Tools

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

# View stats
stats: {}

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
3. Adds core memory to system prompt
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

## Web Panel

Access the context engineering dashboard at `/plugins/context_engineer/panel`:

- View current token usage
- Browse core memory facts
- Search archived messages
- See compaction statistics

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
    └── archival.db           # SQLite archived messages + FTS
```

## Token Estimation

The plugin uses `estimate_content_tokens()` from `agent_system.llm.token_utils`, a character-based estimate (3.3 characters per token, 2.85 for JSON) fitted to real prompt tokens of the production models. The hook compares it with the provider's count of the previous call and uses the larger of the two.

## Best Practices

1. **Add facts proactively** - Use `store_fact` to preserve important information before it's compacted
2. **Reference stored content** - Leave a ref where the content was, so the LLM can fetch it on demand
3. **Monitor stats** - Use the web panel or `stats` tool to track context usage
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
