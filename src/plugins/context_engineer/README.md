# Context Engineer Plugin

Advanced context window optimization for long-running agent conversations using layered compaction strategies inspired by LLMLingua, MemGPT, and Anthropic's context management patterns.

## Overview

The Context Engineer plugin provides intelligent context management to prevent token limits from being exceeded during long conversations. It implements a multi-layered approach:

1. **Tool Result Store** - Compact references to tool outputs
2. **Variable Manager** - $VAR_N substitution for large content blocks
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
│  │   Core Memory   │  │  Tool Result    │  │    Variable     │     │
│  │  (always in     │  │    Store        │  │    Manager      │     │
│  │   context)      │  │  (SQL refs)     │  │  ($VAR_N refs)  │     │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘     │
│           │                    │                    │               │
│           └────────────────────┼────────────────────┘               │
│                                │                                    │
│                    ┌───────────▼───────────┐                        │
│                    │  Layered Compaction   │                        │
│                    │  L1: Reversible       │                        │
│                    │  L2: Semi-reversible  │                        │
│                    │  L3: Irreversible     │                        │
│                    └───────────┬───────────┘                        │
│                                │                                    │
│                    ┌───────────▼───────────┐                        │
│                    │   Archival Memory     │                        │
│                    │  (FTS5 + ChromaDB)    │                        │
│                    └───────────────────────┘                        │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Hook: pre_llm_call  │  Tools: list, read, store_fact,             │
│                      │         stats, compact  (recall: deprecated) │
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

### Variable Manager (`variable_manager.py`)

Creates $VAR_N references for large content blocks like code files, documents, or data. The LLM can reference variables by name.

```python
from plugins.context_engineer.variable_manager import VariableManager

vm = VariableManager(min_content_tokens=500)

# Create variable for large content
var_name, summary = vm.create_variable(
    content="def process_data():\n    # ... 500 lines ...",
    content_type="code",
    source="main.py"
)
# Returns: ("$VAR_1", "[Python code: function process_data, ~500 lines]")

# Expand variables in text
expanded = vm.expand_variables("Look at $VAR_1 for the implementation")
```

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

**Layer 1 (Reversible):**
- Store tool outputs with compact references
- Create variables for large content blocks
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
    tool_store, variable_manager, core_memory, archival_memory, config
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
| `stats` | Get current context statistics |
| `compact` | Manually trigger compaction |
| `recall` | **Deprecated** — the single guessing tool these two replace |

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
| `$VAR_n` | a stored content block |
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

# Find it by keyword (messages, tool outputs, variables and facts)
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

# Manual compaction
compact:
  target_tokens: 50000
```

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
    
    # Variables
    variable_min_size: 500
    
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
```

## Web Panel

Access the context engineering dashboard at `/plugins/context_engineer/panel`:

- View current token usage
- Browse core memory facts
- Search archived messages
- View stored variables
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

### context_optimizer
For model-specific token limits, context_optimizer handles the model selection while context_engineer manages the content optimization.

## Storage Locations

Data is stored under `data/context_engineer/{session_id}/`:

```
data/context_engineer/
└── {session_id}/
    ├── core_memory.json      # Important facts
    ├── variables.json        # Variable store
    ├── tool_results.db       # SQLite tool outputs
    └── archival.db           # SQLite archived messages + FTS
```

## Token Estimation

The plugin uses `estimate_content_tokens()` from `agent_system.llm.token_utils` which provides word-based estimation (~1.3 tokens per word). For more accurate counting, the LLM provider's tokenizer can be used.

## Best Practices

1. **Add facts proactively** - Use `store_fact` to preserve important information before it's compacted
2. **Reference variables** - Tell the LLM about available variables so it can request content when needed
3. **Monitor stats** - Use the web panel or `stats` tool to track context usage
4. **Tune thresholds** - Adjust compaction thresholds based on your model's context window
5. **Use semantic search** - Enable ChromaDB for better archival retrieval in long conversations

## Error Handling

The plugin handles errors gracefully:
- If archival storage fails, messages are kept in context
- If compaction fails, original messages are preserved
- Missing variables/tool results return helpful error messages

## Future Enhancements

- [ ] LLM-based summarization for archived messages
- [ ] Automatic importance scoring for facts
- [ ] Cross-session memory sharing
- [ ] Token budget prediction based on conversation patterns
