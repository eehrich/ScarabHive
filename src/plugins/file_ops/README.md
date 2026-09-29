# File Operations Plugin

## Overview

The **File Operations Plugin** provides secure, powerful file system access for AgentSystem. It offers comprehensive tools for file management, search, and content manipulation with built-in security protections, index-free search, and atomic write guarantees.

**Key Features:**
- ✅ **File Operations**: Read, create, edit, delete, move, rename files and directories
- 🔒 **Security-First Design**: Path traversal protection, symlink validation, allowed directory whitelist
- ⚡ **Index-Free Search**: Glob and grep walk the disk on demand, pruning `.gitignore`d and excluded directories — no index to wait for
- 💾 **Atomic Writes**: Temp file + rename pattern prevents corruption
- 📄 **Pagination Support**: Efficient handling of large files with offset/limit
- 🎯 **Advanced Search**: Glob patterns, regex, context lines, and filtering

## Installation

The plugin is included in AgentSystem by default. Enable it in `config/plugins.yaml`:

```yaml
plugins:
  servers:
    file_ops:
      type: file_ops
      enabled: true
      allowed_directories:
        - src
        - docs
        - tests
        - data/workspace
      max_file_size_mb: 10
      search:
        exclude_patterns:
          - "**/.git/**"
          - "**/__pycache__/**"
          - "**/node_modules/**"
```

## Available Tools

### 1. `file_ops_read_file`

Read text file contents with optional pagination.

**Parameters:**
- `file_path` (string, required): Absolute path to file
- `offset` (integer, optional): Starting line number (0-indexed, default: 0)
- `limit` (integer, optional): Maximum lines to read (default: read all)
- `encoding` (string, optional): Text encoding (default: "utf-8")

**Example:**
```json
{
  "file_path": "/project/src/main.py",
  "offset": 10,
  "limit": 50
}
```

**Response:**
```json
{
  "status": "success",
  "content": "...",
  "total_lines": 347,
  "lines_read": 50,
  "offset": 10,
  "encoding": "utf-8",
  "file_path": "/project/src/main.py"
}
```

---

### 2. `file_ops_manage`

Unified file/directory management: create, delete, move, rename.

**Parameters:**
- `operation` (string, required): Operation to perform: `create`, `delete`, `move`, `rename`
- `path` (string, required): Absolute path to the file or directory
- `content` (string, conditional): File content (required for `create`)
- `destination` (string, conditional): Destination path (required for `move`)
- `new_name` (string, conditional): New name without path (required for `rename`)
- `overwrite` (boolean, optional): Replace an existing file whole (for `create`, default: false)
- `recursive` (boolean, optional): Allow recursive deletion of non-empty directories (default: false)

**Create Example:**
```json
{
  "operation": "create",
  "path": "/project/tmp/output.txt",
  "content": "Hello World\nLine 2"
}
```

**Response (create):**
```json
{
  "status": "success",
  "file_path": "/project/tmp/output.txt",
  "bytes_written": 18,
  "replaced": false
}
```

**Delete Example (file):**
```json
{
  "operation": "delete",
  "path": "/project/tmp/old_file.txt"
}
```

**Delete Example (non-empty directory):**
```json
{
  "operation": "delete",
  "path": "/project/tmp/old_dir",
  "recursive": true
}
```

**Response (delete):**
```json
{
  "status": "success",
  "path": "/project/tmp/old_file.txt",
  "type": "file",
  "message": "File deleted successfully"
}
```

**Move Example:**
```json
{
  "operation": "move",
  "path": "/project/src/old_location/file.py",
  "destination": "/project/src/new_location/file.py"
}
```

**Response (move):**
```json
{
  "status": "success",
  "source": "/project/src/old_location/file.py",
  "destination": "/project/src/new_location/file.py",
  "type": "file",
  "message": "File moved successfully"
}
```

**Rename Example:**
```json
{
  "operation": "rename",
  "path": "/project/src/old_name.py",
  "new_name": "new_name.py"
}
```

**Response (rename):**
```json
{
  "status": "success",
  "old_path": "/project/src/old_name.py",
  "new_path": "/project/src/new_name.py",
  "type": "file",
  "message": "File renamed successfully"
}
```

**Behavior Notes:**
- `create`: Fails if the file exists unless `overwrite: true`, which replaces it whole in one call; auto-creates parent directories. A path that is a directory is refused.
- `delete`: Fails for non-empty directories unless `recursive: true`
- `move`: Fails if destination exists, auto-creates parent directories
- `rename`: Fails if target name exists, keeps file in same directory

---

### 3. `file_ops_replace_string_in_file`

Replace exact string match in a file (VSCode/Copilot-style precise string replacement).

**Parameters:**
- `filePath` (string, required): Absolute path to file
- `oldString` (string, required): Exact text to find (include context for uniqueness)
- `newString` (string, required): Replacement text

**Example:**
```json
{
  "filePath": "/project/config.yaml",
  "oldString": "debug: false",
  "newString": "debug: true"
}
```

**Response:**
```json
{
  "status": "success",
  "file_path": "/project/config.yaml",
  "mode": "replace",
  "changes": {
    "replacements": 1
  }
}
```

**Notes:**
- `oldString` must match exactly including whitespace, and exactly **once**: several matches are refused with `error_type: AmbiguousMatchError` and the count, nothing is written
- Include 3+ lines of context to ensure unique match
- `\n` in `oldString` also matches a CRLF file, and the file keeps its line endings
- This is the only edit primitive — there are no line-number edits: line numbers go stale after the first edit above them

---

### 4. `file_ops_list_directory`

List directory contents with optional filtering and recursion.

**Parameters:**
- `dir_path` (string, required): Absolute path to directory
- `recursive` (boolean, optional): Recursive listing (default: false)
- `pattern` (string, optional): Glob filter pattern (e.g., "*.py")
- `include_hidden` (boolean, optional): Include hidden files (default: false)
- `max_results` (integer, optional): Maximum entries, files + directories (default: 200, at most 1000)
- `include_ignored` (boolean, optional): Also list ignored, excluded and hidden entries (default: false)

A recursive listing uses the same pruning walk as the searches: `.gitignore`d and excluded directories are not entered, and `skipped` says so. A cut-off listing has `truncated: true`.

**Example:**
```json
{
  "dir_path": "/project/src",
  "recursive": true,
  "pattern": "*.py",
  "include_hidden": false
}
```

**Response:**
```json
{
  "status": "success",
  "dir_path": "/project/src",
  "files": [
    "/project/src/main.py",
    "/project/src/models/user.py"
  ],
  "directories": [
    "/project/src/models"
  ],
  "total_files": 2,
  "total_directories": 1,
  "truncated": false,
  "skipped": {"filters_active": ["..."], "notes": ["..."]}
}
```

---

### 5. `file_ops_search_files`

Search for files by glob pattern. Walks the allowed directories on demand — see [Search Architecture](#search--indexing-architecture).

**Parameters:**
- `pattern` (string, required): Glob pattern (e.g., `*.py`, `**/*.yaml`, `src/**/models/*.py`)
- `max_results` (integer, optional): Maximum results to return (default: 50)
- `include_ignored` (boolean, optional): Also search what `.gitignore`, `exclude_patterns` and the hidden-file rule hide (default: false)

**Pattern rules:** a pattern without `/` matches the file name at any depth (`*.py`). A pattern with `/` is matched against the path relative to the allowed directory, also with an implicit `**/` in front (`hooks/*.py` works from above). `**/` means zero or more directories, `*` never crosses a `/`. Matching ignores case; `./` and backslashes are accepted.

**Example:**
```json
{
  "pattern": "**/*.py",
  "max_results": 50
}
```

**Response:**
```json
{
  "status": "success",
  "files": [
    "/project/src/main.py",
    "/project/src/utils.py",
    "/project/tests/test_main.py"
  ],
  "total_found": 3,
  "truncated": false,
  "skipped": {
    "filters_active": [".gitignore and .ignore files", "7 configured exclude patterns", "hidden files"],
    "notes": ["Filters active: ... (include_ignored=true searches them too)."]
  }
}
```

`skipped` is there so an empty result can be read correctly: when nothing matched while filters were active, the note says so instead of letting it look like absence.

---

### 6. `file_ops_grep_search`

Search file contents for text/regex patterns with context lines.

**Parameters:**
- `query` (string, required): Search query (literal text or regex pattern)
- `is_regex` (boolean, optional): Treat query as regex pattern (default: false)
- `include_pattern` (string, optional): Only search in files matching this glob (e.g., `**/*.py`)
- `case_sensitive` (boolean, optional): Case-sensitive search (default: false)
- `max_results` (integer, optional): Maximum number of matches to return (default: 100)
- `context_lines` (integer, optional): Number of context lines before/after match (default: 2)
- `include_ignored` (boolean, optional): Also search ignored, excluded and hidden files (default: false)

Binary files (decided by content, not extension) and files above `max_file_size_for_indexing_kb` are skipped and counted in `skipped.notes`.

**Example (literal search):**
```json
{
  "query": "TODO",
  "case_sensitive": false,
  "context_lines": 2,
  "include_pattern": "**/*.py"
}
```

**Example (regex search):**
```json
{
  "query": "def\\s+\\w+\\(",
  "is_regex": true,
  "include_pattern": "src/**/*.py",
  "max_results": 50
}
```

**Response:**
```json
{
  "status": "success",
  "matches": [
    {
      "file_path": "/project/src/main.py",
      "line_number": 45,
      "line_content": "    # TODO: Implement feature",
      "context_before": ["def process():", "    \"\"\"Process data.\"\"\""],
      "context_after": ["    pass"]
    }
  ],
  "total_matches": 1,
  "total_files": 12,
  "truncated": false,
  "skipped": {"filters_active": ["..."], "notes": ["..."]}
}
```

---

### 7. `file_ops_semantic_search`

Finds code by meaning instead of by wording. Every hit is a **symbol** — a
function, a class, a heading section — with its file, its line and its
signature.

**What makes it semantic?**
- Understands context: "how a run is cancelled" finds `_handle_cancel()`
- Language-independent: Finds concepts across different naming conventions
- Fuzzy matching: Finds related code even with different terminology

**Why symbols and not files** (measured 18.09.2026 on this repository): the
embedding model reads 256 tokens and drops the rest silently. A whole file as
one document is therefore indexed by its module head alone — which is why the
old version answered code questions with READMEs. A 50-line window is no
better (95 % of them exceed 256 tokens; at 30 lines still 81 %). A symbol has
a median of 59 tokens, so what the index holds is what the model read.

**Parameters:**
- `query` (string, required): Natural language search query
- `max_results` (integer, optional): Maximum symbols to return (default: 10)
- `filter_pattern` (string, optional): Glob pattern to filter results (e.g., "*.py")

**Example:**
```json
{
  "query": "authentication and login logic",
  "max_results": 5,
  "filter_pattern": "**/*.py"
}
```

**Response:**
```json
{
  "status": "success",
  "query": "authentication and login logic",
  "results": [
    {
      "file_path": "/project/src/auth.py",
      "filename": "auth.py",
      "line": 42,
      "symbol": "def verify_credentials(username, password)",
      "similarity_score": 0.5263,
      "distance": 0.8999,
      "size_bytes": 2048,
      "extension": ".py"
    },
    {
      "file_path": "/project/src/session.py",
      "filename": "session.py",
      "line": 17,
      "symbol": "class SessionStore()",
      "similarity_score": 0.4102,
      "distance": 1.4378,
      "size_bytes": 1536,
      "extension": ".py"
    }
  ],
  "count": 2,
  "max_results": 5
}
```

**Understanding Scores:**
- `similarity_score`: 0.0-1.0 (higher = more relevant)
  - > 0.5: Highly relevant
  - 0.3-0.5: Moderately relevant
  - < 0.3: Weakly relevant
- `distance`: Raw ChromaDB distance (lower = more similar)
  - Formula: similarity = 1 / (1 + distance)

**Requirements:**
- Requires ChromaDB: `pip install chromadb` (Windows falls back to sqlite-vec)
- Off unless configured (`search.enable_semantic_search: true`); a disabled instance answers with `error_type: SemanticSearchDisabled`, not with an empty result
- The first search never waits for the index. While the background build is
  running the answer is `error_type: IndexNotReady` and names `grep_search` —
  an empty result would read as a verdict about the code
- A build that fails (no embedding model, say) is retried every minute; until
  one succeeds, `IndexNotReady` names the last failure's cause instead of
  promising the index "in a minute"

**Performance** (measured on this repository, the coder instance's four roots):
- 3.295 indexable files → 51.730 documents, 288 MB of store
- First build 423 s, in the background; an incremental pass over the same tree
  takes 0.4 s, and a restart reads the state file instead of rebuilding
- Search: ~100-500 ms
- Index stored in `data/cache/file_ops_chromadb/`, one collection per instance
  (`file_ops_<instance>`) plus a `<collection>_state.json` next to it

**Tips:**
- Use specific queries: "database connection pooling" > "database"
- Combine with filter_pattern for faster results
- Similarity > 0.4 usually indicates good match
- Results sorted by similarity (best first)
- `grep_search` is better whenever you know the word the code uses; this tool
  is for when you do not

## Configuration Reference

### Plugin Configuration (`config/plugins.yaml`)

```yaml
file_ops:
  type: file_ops
  enabled: true
  
  # Security: Allowed directory whitelist
  allowed_directories:
    - src           # Source code
    - docs          # Documentation
    - tests         # Test files
    - tmp           # Temporary files
    - config        # Configuration files
    - data          # Data files
  
  # Read-only mode: disables all write operations
  # When enabled, manage (create/delete/move/rename) and replace_string_in_file
  # are blocked and hidden from the schema
  read_only: false
  
  # File size limit (prevents reading huge files)
  max_file_size_mb: 10
  
  # Search configuration
  search:
    search_hidden: false                 # Also search dotfiles/dot-directories
    enable_semantic_search: false        # Semantic index (ChromaDB); off by default
    enable_indexing: true                # false = read an index another instance builds
    collection_name: file_ops_<instance> # Share it to share one index over one tree
    auto_reindex_interval: 300           # Semantic index refresh, seconds
    exclude_patterns:                    # Pruned by search and index, relative to each allowed dir
      - "**/.git/**"
      - "**/__pycache__/**"
      - "**/node_modules/**"
      - "**/.venv/**"
      - "**/*.min.js"
      - "**/*.min.css"
      - "**/*.map"
```

#### Read-Only Mode

When `read_only: true` is set:

1. **Tool Visibility**: The `file_ops_manage` and `file_ops_replace_string_in_file` tools are **hidden from the schema** and won't appear in tool listings
2. **Runtime Protection**: Even if tools are called directly, they return an error: `"Plugin is in read-only mode. Write operations are disabled."`
3. **Read Operations**: `read_file`, `list_directory`, `search_files`, and `grep_search` remain fully functional

**Use Case**: Safe browsing mode for agents that should only analyze code without making changes.

### Agent Tool Access

Add to agent configurations in `config/agents/*.yaml`:

```yaml
agent_config:
  tools:
    allowed:
      - "file_ops_*"  # All file operations tools
```

## Security Model

### Path Validation

The plugin implements defense-in-depth security:

1. **Allowed Directories Whitelist**
   - Only paths within configured `allowed_directories` are accessible
   - Paths are resolved to absolute paths and checked against whitelist
   - Protects against directory traversal attacks

2. **Path Traversal Protection**
   - Blocks `..` sequences in paths
   - Prevents tilde expansion (`~`)
   - Rejects null bytes (`\0`)

3. **Symlink Validation**
   - Follows symlinks and validates final target
   - Ensures symlink targets are also within allowed directories
   - Prevents symlink-based directory escapes

**Example Security Checks:**
```python
# ✅ ALLOWED
/project/src/main.py                  # Within allowed directory

# ❌ BLOCKED
/project/src/../../../etc/passwd      # Path traversal
/project/src/~/secrets.txt            # Tilde expansion
/etc/shadow                           # Outside allowed directories
/project/src/link_to_etc              # Symlink to forbidden directory
```

### Atomic Write Safety

All file writes use atomic operations:

1. Write content to temporary file: `/path/file.txt.tmp_RANDOM`
2. On success, atomically rename to target: `/path/file.txt`
3. On failure, temp file is cleaned up

**Benefits:**
- No partial writes on errors
- Original file preserved until write succeeds
- Crash-safe file operations

## Search & Indexing Architecture

### Glob and grep: no index

`search_files` and `grep_search` walk the allowed directories on every call (`textsearch.py`). There is no index to build or to go stale, so a file written a second ago is found.

What keeps that affordable on a large tree:

- **Pruning, not filtering.** Directories matched by `exclude_patterns` or by the root's `.gitignore`/`.ignore` are never entered. On this repository (152,664 files) a glob or grep over the whole tree answers in about a second.
- **Overlapping roots collapse.** `[., src, data/workspace]` is walked once, as `.`.
- **Configured roots stay visible.** An allowed directory is never hidden by a filter of an allowed directory above it — `.gitignore`, hidden-name rule or exclude pattern (`data/workspace/` is gitignored by the repository, and still searchable when both are allowed). One the filters do not hide is searched under the parent's rules as usual.
- **Symlinks.** Symlinked files are found when their target lies inside an allowed directory; symlinked directories and junctions are not entered.
- **Case.** Search patterns ignore case; exclude patterns do not.
- **Off the event loop.** The walk runs in a worker thread, so a long search does not stall the server's other tool calls.

`include_ignored: true` lifts `.gitignore`, `exclude_patterns` and the hidden-file rule (`.git` stays pruned) — complete, but a full walk: minutes for a grep over a large tree. Every answer lists the active filters in `skipped`.

There is deliberately one implementation. A ripgrep backend was built and measured: it answered differently (globs relative to the process directory, binary matches, a different regex dialect) for speed the walker does not need.

### Semantic index

Only `semantic_search` uses an index, and it holds one document per **symbol**
(`symbols.py`: Python functions and classes via `ast`, Markdown heading
sections, overlapping line windows for everything else), walked with the same
pruning rules. Files above `max_file_size_for_indexing_kb` and binary files are
not indexed.

The build runs in the **background**, started by the first `semantic_search`
call and repeated incrementally by file mtime (`auto_reindex_interval`). The
search itself never waits for it. What each file contributed is written next to
the vectors (`<collection>_state.json`), so a restart picks the index up
instead of paying for the whole tree again — and a state file that disagrees
with its collection, or was written for another document format, is dropped
rather than trusted. (Format 2, 28.09.2026: a document names its file relative
to the indexed directory; an index built before is rebuilt once.)

Each instance owns its collection (`file_ops_<instance>`), because two
instances are two trees: with one shared name the second instance's full
rebuild cleared the first one's index. Instances that deliberately share a tree
set the same `collection_name` and switch `enable_indexing: false` on all but
one of them — that one builds, the others only read (this is what the coder
harness does with `coder_fs` and `coder_fs_ro`).

### Configuration

Add custom exclusions:
```yaml
search:
  exclude_patterns:
    - "**/build/**"
    - "**/*.log"
    - "**/vendor/**"
```

## CLI Usage

Run commands directly from the command line for testing:

```bash
# Activate virtual environment
source .venv/Scripts/activate

# Read a file
python -m plugins.file_ops read /path/to/file.txt

# Read with pagination
python -m plugins.file_ops read /path/to/file.txt --offset 10 --limit 50

# List directory
python -m plugins.file_ops list /path/to/dir --recursive --pattern "*.py"

# Search files by name
python -m plugins.file_ops search "*.py" --search-dir /project/src --max-results 50

# Grep search (literal)
python -m plugins.file_ops grep "TODO" --context 2 --include-pattern "**/*.py"

# Grep search (regex)
python -m plugins.file_ops grep "def\s+\w+\(" --regex --include-pattern "src/**/*.py"
```

**CLI Notes:**
- CLI automatically allows the parent directory of target files
- JSON output suitable for piping to `jq` or other tools

## Error Handling

All tools return structured error responses:

```json
{
  "status": "error",
  "error": "Human-readable error message",
  "error_type": "SecurityError",  // Exception class name
  "file_path": "/attempted/path"
}
```

**Common Error Types:**

| Error Type | Description | Common Causes |
|------------|-------------|---------------|
| `SecurityError` | Security validation failed | Path traversal, outside allowed dirs, symlink to forbidden location |
| `FileNotFoundError` | File does not exist | Typo in path, file deleted |
| `FileExistsError` | File already exists | Attempting to create without `overwrite: true` |
| `PermissionError` | Insufficient permissions | Read-only filesystem, no write access |
| `EncodingError` | Text encoding issue | Binary file read as text, wrong encoding specified |
| `ValueError` | Invalid parameter | Invalid edit mode, negative offset |

## Troubleshooting

### Files Not Found in Search

**Problem:** `search_files` or `grep_search` returns no results for known files.

**Solutions:**
1. Read `skipped` in the answer: it names the active filters (`.gitignore`, exclude patterns, hidden files) and counts binary and oversized files.

2. Repeat with `include_ignored: true` — it lifts `.gitignore`, `exclude_patterns` and the hidden-file rule.

3. To search dotfiles on every call, set `search.search_hidden: true`.

### Security Errors for Valid Paths

**Problem:** `SecurityError: Path outside allowed directories` for legitimate files.

**Solutions:**
1. Add directory to whitelist:
   ```yaml
   allowed_directories:
     - src
     - docs
     - your_new_dir  # Add this
   ```

2. Use absolute paths (relative paths may resolve incorrectly):
   ```json
   {"file_path": "/absolute/path/to/file"}
   ```

3. Check for symlinks pointing outside allowed directories:
   ```bash
   ls -la /path/to/file  # Check if it's a symlink
   ```

### Large File Performance

**Problem:** Slow reads or high memory usage with large files.

**Solutions:**
1. Use pagination:
   ```json
   {"file_path": "/large/file.txt", "offset": 0, "limit": 100}
   ```

2. Increase `max_file_size_mb` if needed:
   ```yaml
   max_file_size_mb: 50  # Allow larger files
   ```

3. Split large files into chunks externally before processing

## Best Practices

### Security
- ✅ Always use absolute paths
- ✅ Limit `allowed_directories` to minimum required
- ✅ Use `confirm: true` for deletions
- ❌ Don't add `/` or `/home` to allowed directories

### Performance
- ✅ Use pagination for large files
- ✅ Exclude generated directories (`**/dist/**`, `**/build/**`) — they are pruned, not filtered

### Reliability
- ✅ Handle error responses gracefully
- ❌ Don't assume write operations succeeded without checking response

## Known Limitations

### Result Limits
- **grep_search**: default 100, at most 500 (`max_results`)
- **search_files**: default 50, at most 500 (`max_results`)
- **semantic_search**: default 10, at most 50 (`max_results`)
- A cut-off answer says so: `truncated: true` plus a note

### File Size Limits
- **Default**: Files > 1MB are skipped by grep and the semantic index (`max_file_size_for_indexing_kb`), and counted in `skipped`
- **Maximum read**: Files > 100MB may cause memory issues
- **Workaround**: Use pagination (`offset`/`limit`) for large files

### Search Accuracy
- **grep_search**: Exact pattern matching only
  - Regex support available but requires careful escaping
  - No fuzzy matching or typo tolerance
  
- **semantic_search**: Requires sufficient context
  - Works best with 10+ tokens of text
  - Short files (< 50 words) may have low similarity scores
  - Not suitable for config files or data-only files
  
- **search_files**: Path matching only
  - No content searching (use grep_search instead)
  - See the pattern rules under the tool description

### Excluded by Default
- `.git`, `__pycache__`, `node_modules`, `.venv`, `*.pyc`, `*.min.js`, `*.min.css`, `*.map`
- Whatever the allowed directory's `.gitignore`/`.ignore` lists
- Hidden files and directories
- Binary files (by content) and files > 1MB, for grep and the semantic index

### Semantic Search Specifics
- **ChromaDB dependency**: Requires `pip install chromadb`
- **First search delay**: Initial index build can take 30-60 seconds
- **Storage**: Index stored in `data/cache/file_ops_chromadb/`
  - Can grow to 100MB+ for large projects
  - No automatic cleanup of old entries
  
- **Similarity interpretation**:
  - > 0.5: Highly relevant (strong keyword/concept match)
  - 0.3-0.5: Moderately relevant (related concepts)
  - < 0.3: Weakly relevant (may be false positive)
  - Scores depend on file content richness

### Performance Considerations
- **Search cost**: proportional to the files that survive pruning, not to the tree
  - `include_ignored: true` walks everything except `.git` — measured on this repository with the coder roots: glob 17 s, grep 8.5 min. Keep the pattern or `include_pattern` narrow.
- **Semantic index**: one document per symbol -- measured on the coder roots,
  3.295 files became 51.730 documents and 288 MB of store

### Platform-Specific
- **Windows**: Path separators auto-converted (`/` → `\\`)
- **Line endings**: Preserved as-is (no auto-conversion)
- **Encoding**: UTF-8 assumed, errors ignored with `errors='ignore'`

## Development

### Running Tests

```bash
# Run all plugin tests
pytest tests/test_plugin_file_ops_basic.py -v

# Run specific test
pytest tests/test_plugin_file_ops_basic.py::test_create_and_read_file -v

# Run with coverage
pytest tests/test_plugin_file_ops_basic.py --cov=plugins.file_ops
```

### Architecture

```
file_ops/
├── __init__.py          # Package initialization
├── plugin.yaml          # Plugin metadata
├── schema.yaml          # Tool definitions
├── plugin.py            # Factory export
├── server.py            # Main tool server (273 lines)
├── security.py          # PathValidator (100 lines)
├── operations.py        # FileOperations (457 lines)
├── search.py            # FileSearchEngine: search seam + semantic index
├── symbols.py           # What the index stores: one document per symbol
├── textsearch.py        # Index-free glob/grep: pruning walk, gitignore, skip report
├── cli.py               # CLI interface (370 lines)
└── __main__.py          # CLI entry point
```

**Component Responsibilities:**
- `PathValidator`: Security validation, path resolution
- `FileOperations`: CRUD operations, atomic writes
- `FileSearchEngine`: Search entry points, semantic index
- `symbols`: Cuts a file into the documents the index holds
- `textsearch`: Walking, glob semantics, ignore rules, what was skipped
- `FileOperationsServer`: tool interface, error handling

## Version History

### v1.0.0 (Current)
- Initial release with 9 tools
- Security-first design with path validation
- Atomic write guarantees
- Comprehensive test coverage (17 tests, 100% pass rate)

## Support

For issues or questions:
- Check this README's troubleshooting section
- Review test files: `tests/test_plugin_file_ops_basic.py`
