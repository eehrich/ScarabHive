# File Operations Plugin

## Overview

The **File Operations Plugin** provides secure, powerful file system access for AgentSystem. It offers comprehensive tools for file management, search, and content manipulation with built-in security protections, background indexing, and atomic write guarantees.

**Key Features:**
- ✅ **File Operations**: Read, create, edit, delete, move, rename files and directories
- 🔒 **Security-First Design**: Path traversal protection, symlink validation, allowed directory whitelist
- ⚡ **Background Indexing**: Fast file search with automatic index rebuilding
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
        enable_indexing: true
        auto_reindex_interval: 300
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
  "bytes_written": 18
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
- `create`: Fails if file exists, auto-creates parent directories
- `delete`: Fails for non-empty directories unless `recursive: true`
- `move`: Fails if destination exists, auto-creates parent directories
- `rename`: Fails if target name exists, keeps file in same directory

---

### 3. `file_ops_create_file`

Create a new file with atomic write guarantee. *(Legacy - prefer `file_ops_manage` with `operation: create`)*

**Parameters:**
- `file_path` (string, required): Absolute path for new file
- `content` (string, required): File content
- `overwrite` (boolean, optional): Allow overwriting existing file (default: false)
- `create_dirs` (boolean, optional): Create parent directories (default: true)
- `encoding` (string, optional): Text encoding (default: "utf-8")

**Example:**
```json
{
  "file_path": "/project/tmp/output.txt",
  "content": "Hello World\nLine 2",
  "overwrite": true,
  "create_dirs": true
}
```

**Response:**
```json
{
  "status": "success",
  "file_path": "/project/tmp/output.txt",
  "bytes_written": 18,
  "encoding": "utf-8"
}
```

**Atomic Write Behavior:**
- File is written to a temporary file first
- Atomic rename ensures no corruption on failure
- Original file preserved if write fails

---

### 4. `file_ops_replace_string_in_file`

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
- `oldString` must match exactly including whitespace
- Include 3+ lines of context to ensure unique match
- Supports CRLF/LF line ending flexibility

---

### 5. `file_ops_list_directory`

List directory contents with optional filtering and recursion.

**Parameters:**
- `dir_path` (string, required): Absolute path to directory
- `recursive` (boolean, optional): Recursive listing (default: false)
- `pattern` (string, optional): Glob filter pattern (e.g., "*.py")
- `include_hidden` (boolean, optional): Include hidden files (default: false)

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
    "main.py",
    "utils.py",
    "models/user.py"
  ],
  "directories": [
    "models",
    "services"
  ],
  "total_files": 3,
  "total_directories": 2,
  "recursive": true,
  "pattern": "*.py"
}
```

---

### 6. `file_ops_search_files`

Search for files by name using glob patterns. Uses background-indexed file name index for fast results.

**Parameters:**
- `pattern` (string, required): Glob pattern (e.g., `*.py`, `**/*.yaml`)
- `max_results` (integer, optional): Maximum results to return (default: 50)

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
  "pattern": "**/*.py",
  "matches": [
    "/project/src/main.py",
    "/project/src/utils.py",
    "/project/tests/test_main.py"
  ],
  "total_found": 3,
  "index_enabled": true
}
```

---

### 7. `file_ops_grep_search`

Search file contents for text/regex patterns with context lines.

**Parameters:**
- `query` (string, required): Search query (literal text or regex pattern)
- `is_regex` (boolean, optional): Treat query as regex pattern (default: false)
- `include_pattern` (string, optional): Only search in files matching this glob (e.g., `**/*.py`)
- `case_sensitive` (boolean, optional): Case-sensitive search (default: false)
- `max_results` (integer, optional): Maximum number of matches to return (default: 100)
- `context_lines` (integer, optional): Number of context lines before/after match (default: 2)

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
  "query": "TODO",
  "is_regex": false,
  "matches": [
    {
      "file_path": "/project/src/main.py",
      "line_number": 45,
      "line_content": "    # TODO: Implement feature",
      "context_before": ["def process():", "    \"\"\"Process data.\"\"\""],
      "context_after": ["    pass"]
    }
  ],
  "total_matches": 1
}
```

---

### 8. `file_ops_semantic_search`

AI-powered semantic code search using ChromaDB embeddings. Finds files by meaning, not just keywords.

**What makes it semantic?**
- Understands context: "authentication logic" finds login/verify functions
- Language-independent: Finds concepts across different naming conventions
- Fuzzy matching: Finds related code even with different terminology

**Parameters:**
- `query` (string, required): Natural language search query
- `max_results` (integer, optional): Maximum results to return (default: 10)
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
      "similarity_score": 0.5263,
      "distance": 0.8999,
      "size_bytes": 2048,
      "extension": ".py"
    },
    {
      "file_path": "/project/src/session.py",
      "filename": "session.py",
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
- Requires ChromaDB: `pip install chromadb`
- Automatic indexing must be enabled (`enable_semantic_search: true`)
- First search triggers index build (may take time for large codebases)

**Performance:**
- Initial indexing: ~50-100 files/second (depends on file size)
- Search: ~100-500ms (cached in ChromaDB)
- Index stored in: `data/cache/file_ops_chromadb/`

**Tips:**
- Use specific queries: "database connection pooling" > "database"
- Combine with filter_pattern for faster results
- Similarity > 0.4 usually indicates good match
- Results sorted by similarity (best first)

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
  
  # Search/indexing configuration
  search:
    enable_indexing: true                # Enable background indexing
    auto_reindex_interval: 300           # Rebuild index every 5 minutes
    exclude_patterns:                    # Patterns to exclude from index
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

### Background Indexing

When `enable_indexing: true`, the plugin maintains two in-memory indexes:

1. **Filename Index** (`file_name_index`)
   - Maps: `lowercase_filename` → `[full_paths]`
   - Used by: `search_files` tool
   - Supports: Fast glob pattern matching

2. **Text Content Index** (`text_index`)
   - Maps: `word` → `[(file_path, line_numbers)]`
   - Used by: `grep_search` tool
   - Tokenization: Words 3+ characters

**Index Lifecycle:**
```
[Start] → Build initial index → Background task runs
           ↓                      ↓
     Serve requests ←────── Auto-rebuild every 5min
```

**Performance:**
- Index rebuild: ~1000 files/second
- Search: O(1) filename lookup, O(log n) text search
- Memory: ~1KB per file indexed

**Excluded from Indexing:**
- Files > 1MB (configurable via `max_file_size_mb`)
- Patterns in `exclude_patterns` config
- Binary files (auto-detected)

### Indexing Configuration

Disable for testing/CLI:
```yaml
search:
  enable_indexing: false
```

Adjust rebuild frequency:
```yaml
search:
  auto_reindex_interval: 600  # 10 minutes
```

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
python -m plugins.file_ops.cli read /path/to/file.txt

# Read with pagination
python -m plugins.file_ops.cli read /path/to/file.txt --offset 10 --limit 50

# Create a file
python -m plugins.file_ops.cli create /path/to/new.txt "Hello World" --overwrite

# Edit file (append)
python -m plugins.file_ops.cli edit /path/to/file.txt --mode append --content "\nNew line"

# Edit file (replace)
python -m plugins.file_ops.cli edit /path/to/file.txt --mode replace \
  --old-string "old text" --new-string "new text"

# Edit file (insert)
python -m plugins.file_ops.cli edit /path/to/file.txt --mode insert \
  --line-number 5 --content "import logging"

# Delete a file
python -m plugins.file_ops.cli delete /path/to/file.txt --confirm

# List directory
python -m plugins.file_ops.cli list /path/to/dir --recursive --pattern "*.py"

# Check existence
python -m plugins.file_ops.cli exists /path/to/file.txt

# Get file info
python -m plugins.file_ops.cli info /path/to/file.txt

# Search files by name
python -m plugins.file_ops.cli search "*.py" --search-dir /project/src --max-results 50

# Grep search (literal)
python -m plugins.file_ops.cli grep "TODO" --context 2 --include-pattern "**/*.py"

# Grep search (regex)
python -m plugins.file_ops.cli grep "def\s+\w+\(" --regex --include-pattern "src/**/*.py"
```

**CLI Notes:**
- CLI automatically allows the parent directory of target files
- Indexing is disabled by default for CLI to avoid delays
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
1. Check if file is excluded:
   ```yaml
   search:
     exclude_patterns:
       - "**/.git/**"      # Git files excluded
       - "**/node_modules/**"  # NPM packages excluded
   ```

2. Verify indexing is enabled:
   ```yaml
   search:
     enable_indexing: true
   ```

3. Manually rebuild index (during development):
   ```python
   await server.search_engine.rebuild_index()
   ```

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

### Index Out of Date

**Problem:** New files not appearing in search results.

**Solutions:**
1. Wait for auto-rebuild (default: 5 minutes)

2. Reduce rebuild interval:
   ```yaml
   search:
     auto_reindex_interval: 60  # Rebuild every minute
   ```

3. Restart agent to force immediate rebuild

## Best Practices

### Security
- ✅ Always use absolute paths
- ✅ Limit `allowed_directories` to minimum required
- ✅ Use `confirm: true` for deletions
- ❌ Don't add `/` or `/home` to allowed directories

### Performance
- ✅ Enable indexing for frequent searches
- ✅ Use pagination for large files
- ✅ Exclude unnecessary directories from indexing
- ❌ Don't index binary/generated files

### Reliability
- ✅ Use `create_file` with `overwrite: false` for safety
- ✅ Check `file_exists` before operations
- ✅ Handle error responses gracefully
- ❌ Don't assume write operations succeeded without checking response

## Known Limitations

### Result Limits
- **grep_search**: Maximum 100 results (configurable via `max_results`)
- **search_files**: Maximum 100 results (configurable via `max_results`)
- **semantic_search**: Maximum 50 results (configurable via `max_results`)
- **Reason**: Prevents memory overflow with large codebases

### File Size Limits
- **Default**: Files > 1MB excluded from indexing
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
  
- **search_files**: Filename-only matching
  - No content searching (use grep_search instead)
  - Pattern must match full path (use `**/` prefix for subdirs)

### Indexing Limitations
- **Auto-rebuild interval**: Minimum 60 seconds recommended
  - Too frequent rebuilds impact performance
  - File changes not immediately searchable
  
- **Excluded by default**: 
  - `.git`, `__pycache__`, `node_modules`, `.venv`
  - Binary files (not auto-detected, only by extension)
  - Files > 1MB
  
- **No incremental updates**: Full rebuild on every cycle
  - Large codebases (10k+ files) may take 10-30 seconds

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
- **Concurrent searches**: Not optimized for parallel requests
  - Multiple simultaneous searches may queue
  - Background indexing blocks search temporarily
  
- **Memory usage**: Proportional to indexed file count
  - ~1KB per file in memory index
  - ChromaDB adds ~10-50KB per file on disk

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
├── server.py            # Main MCP server (273 lines)
├── security.py          # PathValidator (100 lines)
├── operations.py        # FileOperations (457 lines)
├── search.py            # FileSearchEngine (355 lines)
├── cli.py               # CLI interface (370 lines)
└── __main__.py          # CLI entry point
```

**Component Responsibilities:**
- `PathValidator`: Security validation, path resolution
- `FileOperations`: CRUD operations, atomic writes
- `FileSearchEngine`: Indexing, search, background tasks
- `FileOperationsServer`: MCP tool interface, error handling

## Version History

### v1.0.0 (Current)
- Initial release with 9 tools
- Security-first design with path validation
- Background indexing for fast search
- Atomic write guarantees
- Comprehensive test coverage (17 tests, 100% pass rate)

## Support

For issues or questions:
- Check this README's troubleshooting section
- Review test files: `tests/test_plugin_file_ops_basic.py`
- Check design docs: `docs/file_ops_plugin_design.md`
