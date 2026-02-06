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
│  Hook: pre_llm_call  │  Tools: recall, store_fact, get_variable,   │
│                      │         get_tool_result, stats, compact      │
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
| `recall` | Search archived conversation history |
| `store_fact` | Add important fact to core memory |
| `get_variable` | Retrieve full content of a variable |
| `get_tool_result` | Retrieve full tool output by reference |
| `stats` | Get current context statistics |
| `compact` | Manually trigger compaction |

### Tool Examples

```yaml
# Recall past conversations
recall:
  query: "database optimization"
  limit: 5

# Store important fact
store_fact:
  fact: "User prefers async code patterns"
  category: "preferences"
  importance: 0.9

# Get variable content
get_variable:
  name: "$VAR_3"

# Get tool result
get_tool_result:
  reference: "call_abc123"

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
