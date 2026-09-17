# Memory Plugin

Persistent memory storage with semantic search for AgentSystem.

## Overview

The Memory Plugin provides long-term memory capabilities for AI agents, allowing them to store, recall, and search information across conversation sessions. It uses ChromaDB for semantic vector search and JSON for metadata persistence.

## Features

- **Semantic Search**: ChromaDB-powered vector search finds related memories by meaning, not just keywords
- **Hybrid Storage**: ChromaDB for embeddings + JSON for metadata (access tracking, timestamps, importance)
- **Access Tracking**: Automatic tracking of memory access counts and timestamps
- **Keyword Extraction**: Auto-extract keywords from memory content (frequency-based)
- **System Prompt Injection**: Optionally inject relevant memories into LLM context (via hook)
- **Panel**: browse, search and delete the memories of a session in the shell
- **Session Isolation**: Memories are scoped to sessions (multi-user support)

## Installation

The Memory Plugin is included in AgentSystem. ChromaDB is automatically installed as a dependency.

```bash
# Already installed if you have AgentSystem
pip install agent-system
```

## Configuration

### Plugin Configuration (`config/plugins.yaml`)

```yaml
plugins:
  memory:
    enabled: true
    type: hybrid  # tools + hooks + Web UI
    config:
      max_memories: 10  # Max memories injected into prompts
      max_memories_per_session: 1000  # Storage limit per session
      auto_extract_keywords: true  # Auto-extract from content
      search_n_results: 5  # Default search result count
      use_semantic_injection: true  # Use semantic search for hook injection
```

### Hook Configuration (`schema.yaml`)

```yaml
hooks:
  inject_memory_context:
    enabled: false  # Disabled by default - enable per agent
    hook_type: pre_llm_call
    priority: 20
```

To enable memory injection for specific agents, add to their configuration:

```yaml
agents:
  my_agent:
    hooks:
      memory.inject_memory_context:
        enabled: true
```

## Usage

### Store a Memory

```python
await agent.call_tool("memory", {
    "operation": "store",
    "title": "Python Best Practices",
    "content": "Always use type hints for function parameters and return values",
    "keywords": ["python", "type-hints", "best-practices"],  # Optional
    "importance": 8,  # 1-10 scale
    "tags": ["python", "coding-standards"]  # Optional
})
```

**Response:**
```json
{
    "memory_id": "mem_20240129_143022_a1b2c3",
    "title": "Python Best Practices",
    "created_at": "2024-01-29T14:30:22.123Z",
    "keywords": ["python", "type-hints", "best-practices"],
    "message": "Memory stored successfully with ID: mem_20240129_143022_a1b2c3"
}
```

### Recall a Memory (Updates Access Count)

```python
await agent.call_tool("memory", {
    "operation": "recall",
    "memory_id": "mem_20240129_143022_a1b2c3"
})
```

**Response:**
```json
{
    "memory_id": "mem_20240129_143022_a1b2c3",
    "title": "Python Best Practices",
    "content": "Always use type hints for function parameters and return values",
    "keywords": ["python", "type-hints", "best-practices"],
    "tags": ["python", "coding-standards"],
    "importance": 8,
    "created_at": "2024-01-29T14:30:22.123Z",
    "accessed_at": "2024-01-29T14:35:10.456Z",
    "access_count": 3
}
```

### Semantic Search

```python
await agent.call_tool("memory", {
    "operation": "search",
    "query": "how to write clean code in Python",
    "n_results": 5  # Optional, default from config
})
```

**Response:**
```json
{
    "query": "how to write clean code in Python",
    "results": [
        {
            "memory_id": "mem_20240129_143022_a1b2c3",
            "title": "Python Best Practices",
            "content": "Always use type hints...",
            "similarity": 0.883,  # 0.0-1.0, higher = better match
            "importance": 8,
            "keywords": ["python", "type-hints", "best-practices"],
            "access_count": 3
        },
        {
            "memory_id": "mem_20240130_091015_d4e5f6",
            "title": "Code Style Guide",
            "content": "Follow PEP 8 for Python...",
            "similarity": 0.756,
            "importance": 7,
            "keywords": ["python", "pep8", "style"],
            "access_count": 1
        }
    ],
    "count": 2
}
```

### List Memories (Paginated, Sorted)

```python
await agent.call_tool("memory", {
    "operation": "list",
    "limit": 50,  # Optional, default 50
    "offset": 0,  # Optional, default 0
    "sort_by": "accessed",  # created | updated | accessed | importance
    "sort_order": "desc"  # asc | desc
})
```

**Response:**
```json
{
    "memories": [
        {
            "memory_id": "mem_20240129_143022_a1b2c3",
            "title": "Python Best Practices",
            "keywords": ["python", "type-hints"],
            "importance": 8,
            "created_at": "2024-01-29T14:30:22.123Z",
            "accessed_at": "2024-01-29T14:35:10.456Z",
            "access_count": 3
        }
    ],
    "total": 25,
    "limit": 50,
    "offset": 0,
    "count": 1
}
```

### Delete a Memory

```python
await agent.call_tool("memory", {
    "operation": "delete",
    "memory_id": "mem_20240129_143022_a1b2c3"
})
```

**Response:**
```json
{
    "deleted": true,
    "memory_id": "mem_20240129_143022_a1b2c3",
    "message": "Memory mem_20240129_143022_a1b2c3 deleted successfully"
}
```

### Update a Memory

Edit an existing memory without deleting and recreating it. Partial updates are supported - only provide the fields you want to change.

```python
await agent.call_tool("memory", {
    "operation": "update",
    "memory_id": "mem_001",
    "title": "Updated Title",  # Optional
    "content": "Updated content with corrections",  # Optional
    "importance": 9,  # Optional
    "tags": ["updated", "corrected"]  # Optional
})
```

**Response:**
```json
{
    "memory_id": "mem_001",
    "title": "Updated Title",
    "updated_fields": ["title", "content", "importance", "tags"],
    "updated_at": "2024-01-29T15:45:30.789Z",
    "message": "Memory mem_001 updated successfully"
}
```

**Note:** When you update the `content` field, the memory is automatically re-indexed in ChromaDB for semantic search.

## Hook: System Prompt Injection

When enabled, the `inject_memory_context` hook automatically injects relevant memories into the system prompt before each LLM call.

**Two injection modes:**

1. **Semantic Search** (`use_semantic_injection: true`): Searches for memories related to the user's current message
2. **Recent Access** (`use_semantic_injection: false`): Injects most recently accessed/important memories

**Example injection:**

```
AVAILABLE MEMORIES (use 'memory' tool with operation='recall' to access):
- [mem_20240129_143022_a1b2c3] Python Best Practices
- [mem_20240130_091015_d4e5f6] Code Style Guide
- [mem_20240131_120030_g7h8i9] Testing Patterns
```

## The panel

**Memory** in the shell's panel launcher (category *Context*), served at `/plugins/<instance>/`. Built on the UI kit
(`templates/panel.html`, `static/panel.js`, `static/panel.css`).

- It shows the memories of the session open in the chat and follows the chat when it switches; a link with
  `?session_id=` (the session context entry, e.g. the info button of a session) keeps it on that session. With no
  session open it says so and asks the server nothing.
- Figures: number of memories, average importance, accesses. Below, the 100 most recently accessed memories with
  title, the start of the content, keywords, importance, accesses and last access; with more, the panel says how many
  there are.
- **Search** runs the semantic search in the session and lists the 20 closest memories with their match. A search
  belongs to its session: switching sessions drops it. The auto refresh (every 5 s, pausable in the toolbar) keeps
  a search's results and only brings the figures, as each search embeds its query anew; the refresh button runs it
  again.
- A row (click, Enter or Space) opens the memory in a drawer with its figures and full content. **Delete** there asks
  first and deletes in the session the memory belongs to.
- Viewing or searching in the panel does not count as an access: the access count and the order of the
  non-semantic prompt injection stay the agents' own.

### Endpoints

All take `session_id` as a query parameter, limited to the characters of a session id (`A-Z a-z 0-9 _ -`,
otherwise 422): it names the metadata file and the vector collection. A metadata file that cannot be read or
written, or a failing vector store, is answered 503 with the reason.

| Method | Path | Answer |
|---|---|---|
| GET | `/` | the panel |
| GET | `/memories` | `{memories: [{memory_id, title, content, keywords, importance, created_at, accessed_at, access_count}], total}` — the 100 most recently accessed |
| GET | `/stats` | `{total_memories, avg_importance, total_accesses}`; `avg_importance` is `null` without memories |
| POST | `/memories/search` | body `{query}` (1–500 characters); `{query, results: [{memory_id, title, content, similarity, importance, keywords, access_count}], count, message}`, the 20 closest |
| DELETE | `/memories/{memory_id}` | `{deleted, memory_id, message}`; 404 for a memory not (or no longer) there |

### Tests

`tests/test_plugin_memory_panel.py` drives the real panel in a headless Chromium (`tests/panel_tests.html`) against
the real plugin and its store under pytest's `tmp_path`; only the vector store is replaced by a word-overlap ranking,
so no embedding model runs. Skipped without a Chromium-based browser.

```bash
pytest src/plugins/memory/tests/test_plugin_memory_panel.py -q
```

## Storage

**File Structure:**
```
data/
  memories/
    test_session_001.json        # Metadata (access counts, timestamps)
    chroma/                       # ChromaDB vector database
      chroma.sqlite3              # Embeddings storage
```

**Metadata Format (`*.json`):**
```json
{
    "session_id": "test_session_001",
    "memories": {
        "mem_20240129_143022_a1b2c3": {
            "memory_id": "mem_20240129_143022_a1b2c3",
            "title": "Python Best Practices",
            "content": "Always use type hints...",
            "keywords": ["python", "type-hints"],
            "session_id": "test_session_001",
            "created_at": "2024-01-29T14:30:22.123Z",
            "updated_at": "2024-01-29T14:30:22.123Z",
            "accessed_at": "2024-01-29T14:35:10.456Z",
            "access_count": 3,
            "importance": 8,
            "tags": ["python", "coding-standards"]
        }
    },
    "total_memories": 1,
    "created_at": "2024-01-29T14:30:22.123Z",
    "updated_at": "2024-01-29T14:35:10.456Z"
}
```

## API Reference

### Tool: `memory`

Single unified tool with operation-based routing.

**Operations:**
- `store`: Create new memory
- `recall`: Retrieve memory by ID (updates access count)
- `search`: Semantic search by query
- `list`: List all memories (paginated, sorted)
- `delete`: Remove memory
- `update`: Edit existing memory (partial updates supported)

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| operation | enum | Yes | - | store \| recall \| search \| list \| delete \| update |
| title | string | For `store`; optional for `update` | - | Memory title (max 200 chars) |
| content | string | For `store`; optional for `update` | - | Memory content (max 5000 chars) |
| keywords | array | No | auto-extracted | List of keywords (for store/update) |
| importance | int | No | 5 | Importance score 1-10 (for store/update) |
| tags | array | No | [] | List of tags (for store/update) |
| memory_id | string | For `recall`, `delete`, `update` | - | Memory ID |
| query | string | For `search` | - | Search query |
| n_results | int | For `search` | 5 | Max results |
| limit | int | For `list` | 50 | Max memories |
| offset | int | For `list` | 0 | Pagination offset |
| sort_by | enum | For `list` | accessed | created \| updated \| accessed \| importance |
| sort_order | enum | For `list` | desc | asc \| desc |

## Architecture

**Components:**
- `MemoryServer`: tool server + hook implementation
- `MemoryWebFactory`: the panel and its endpoints
- `MemoryManagementHybridPlugin`: Plugin factory (hybrid: MCP + hooks + web)

**Data Models:**
- `Memory`: Individual memory record
- `MemoryCollection`: Session's memory collection

**Exceptions:**
- `MemoryError`: Base exception
- `ValidationError`: Invalid parameters
- `StorageError`: File I/O errors
- `ChromaDBError`: Vector DB errors

## Development

### Run Tests

```bash
pytest src/plugins/memory/tests -q
```

### Test ChromaDB Integration

```python
from plugins.memory.server import MemoryServer

server = MemoryServer("memory", system_config, server_config)

# Store
await server._operation_store(
    session_id="test",
    title="Test Memory",
    content="This is a test"
)

# Search
results = await server._operation_search(
    session_id="test",
    query="test memory",
    n_results=5
)
```

## Troubleshooting

**ChromaDB not found:**
```bash
pip install chromadb>=0.4.0
```

**Memory not persisting:**
- Check `data/memories/` directory exists
- Verify session IDs match
- Check file permissions

**Search returns no results:**
- ChromaDB creates embeddings asynchronously
- Try recalling memory first to verify storage
- Check session isolation (different session = different ChromaDB collection)

**Hook not injecting memories:**
- Verify hook is enabled in agent config
- Check `use_semantic_injection` setting
- Ensure memories exist for session

## License

Part of AgentSystem - MIT License
