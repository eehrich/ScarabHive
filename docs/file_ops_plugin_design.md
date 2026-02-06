# File Operations Plugin - Design Document

## Overview

The `file_operations` plugin provides a secure MCP server for text file manipulation operations. This plugin enables agents to create, read, edit, and delete text files within sandboxed directories, similar to GitHub Copilot's file manipulation capabilities.

## Design Goals

1. **Security First**: Prevent path traversal attacks and restrict access to configured directories
2. **Agent-Friendly**: Provide intuitive tools that agents can use effectively for coding tasks
3. **Safe Operations**: Atomic writes with precise string replacement (like GitHub Copilot)
4. **Large File Support**: Handle large files efficiently with offset/limit parameters
5. **Cross-Platform**: Work consistently on Windows and Linux

## Plugin Metadata

```yaml
# plugin.yaml
name: file_operations
version: 1.0.0
description: "Secure file operations for text files with sandboxed directory access"
author: AgentSystem Team
entrypoint: server:FileOperationsServer
type:
  - mcp-server
category: utilities
```

## Architecture

### Components

```
src/plugins/file_operations/
├── plugin.yaml          # Plugin metadata
├── schema.yaml          # Tool definitions
├── server.py           # Main MCP server (FileOperationsServer)
├── security.py         # Path validation and security
├── operations.py       # File operation implementations
├── cli.py              # CLI support
├── README.md           # Documentation
└── __init__.py
```

### Class Structure

```python
# server.py
class FileOperationsServer(SchemaBasedMCPServer):
    """MCP server providing secure file operations."""

    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        # Initialize with allowed directories from config
        pass

    async def read_file(self, params: dict) -> dict: ...
    async def create_file(self, params: dict) -> dict: ...
    async def edit_file(self, params: dict) -> dict: ...
    async def delete_file(self, params: dict) -> dict: ...
    async def list_directory(self, params: dict) -> dict: ...
    async def search_files(self, params: dict) -> dict: ...
    async def grep_search(self, params: dict) -> dict: ...
    async def semantic_search(self, params: dict) -> dict: ...

# security.py
class PathValidator:
    """Validates and sanitizes file paths."""

    def __init__(self, allowed_dirs: list[str]):
        self.allowed_dirs = [Path(d).resolve() for d in allowed_dirs]

    def validate_path(self, path: str) -> Path:
        """Validate path is safe and within allowed directories."""
        # Resolve absolute path
        # Check for path traversal
        # Verify within allowed directories
        # Handle symlinks safely
        pass

# operations.py
class FileOperations:
    """Safe file operation implementations."""

    async def read_file_safe(self, path: Path, offset: int = 0, limit: int = None) -> dict: ...
    async def create_file_safe(self, path: Path, content: str, overwrite: bool = False) -> dict: ...
    async def edit_file_safe(self, path: Path, mode: str, **kwargs) -> dict: ...
    async def delete_file_safe(self, path: Path) -> dict: ...

# search.py
class FileSearchEngine:
    """Fast file search with background indexing (like GitHub Copilot's semantic_search)."""

    def __init__(self, allowed_dirs: list[Path]):
        self.allowed_dirs = allowed_dirs
        self.index: dict[str, list[Path]] = {}  # token -> [file paths]
        self.file_mtimes: dict[Path, float] = {}  # Track file modifications
        self._indexing_task = None

    async def search_files(self, pattern: str, max_results: int = 50) -> list[Path]: ...
    async def grep_search(self, query: str, is_regex: bool = False, max_results: int = 100) -> list[dict]: ...
    async def rebuild_index(self) -> None: ...
    async def update_index_incremental(self) -> None: ...
```

## Tool Definitions

### 1. read_file

**Purpose**: Read text file contents with optional pagination for large files.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_read_file"
    description: "Read contents of a text file. Supports offset/limit for large files."
    parameters:
      type: object
      properties:
        file_path:
          type: string
          description: "Absolute or relative path to file"
        offset:
          type: integer
          minimum: 0
          description: "Start reading from this line number (0-indexed)"
        limit:
          type: integer
          minimum: 1
          description: "Maximum number of lines to read"
        encoding:
          type: string
          default: "utf-8"
          description: "Text encoding (utf-8, latin-1, etc.)"
      required: ["file_path"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "content": "file content here...",
  "total_lines": 1523,
  "lines_read": 100,
  "offset": 0,
  "encoding": "utf-8",
  "file_path": "/absolute/path/to/file.txt"
}
```

### 2. manage (Unified File/Directory Operations)

**Purpose**: Unified tool for file/directory management - create, delete, move, rename.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_manage"
    description: "Unified file/directory management: create, delete, move, rename."
    parameters:
      type: object
      properties:
        operation:
          type: string
          enum: ["create", "delete", "move", "rename"]
          description: "Operation to perform"
        path:
          type: string
          description: "Absolute path to file or directory"
        content:
          type: string
          description: "File content (required for create)"
        destination:
          type: string
          description: "Destination path (required for move)"
        new_name:
          type: string
          description: "New name without path (required for rename)"
        recursive:
          type: boolean
          default: false
          description: "Allow recursive deletion of non-empty directories"
      required: ["operation", "path"]
      additionalProperties: false
```

**Response (create)**:
```json
{
  "status": "success",
  "file_path": "/project/new_file.txt",
  "bytes_written": 1234
}
```

**Response (delete)**:
```json
{
  "status": "success",
  "path": "/project/old_file.txt",
  "type": "file",
  "message": "File deleted successfully"
}
```

**Response (move)**:
```json
{
  "status": "success",
  "source": "/project/old/file.txt",
  "destination": "/project/new/file.txt",
  "type": "file",
  "message": "File moved successfully"
}
```

**Response (rename)**:
```json
{
  "status": "success",
  "old_path": "/project/old_name.txt",
  "new_path": "/project/new_name.txt",
  "type": "file",
  "message": "File renamed successfully"
}
```

### 3. create_file (Legacy)

**Purpose**: Create a new text file with content. *(Prefer `manage` with `operation: create`)*

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_create_file"
    description: "Create a new text file. Fails if file already exists unless overwrite=true."
    parameters:
      type: object
      properties:
        file_path:
          type: string
          description: "Path where file will be created"
        content:
          type: string
          description: "File content to write"
        overwrite:
          type: boolean
          default: false
          description: "Allow overwriting existing file"
        create_dirs:
          type: boolean
          default: true
          description: "Auto-create parent directories"
        encoding:
          type: string
          default: "utf-8"
          description: "Text encoding"
      required: ["file_path", "content"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "file_path": "/absolute/path/to/new_file.txt",
  "bytes_written": 1234,
  "created_dirs": ["/path/to"]
}
```

### 4. replace_string_in_file

**Purpose**: Replace exact string match in a file (VSCode/Copilot-style).

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_replace_string_in_file"
    description: "Replace exact string in file. Include context for unique matching."
    parameters:
      type: object
      properties:
        filePath:
          type: string
          description: "Path to file to edit"
        oldString:
          type: string
          description: "Exact text to find (include context for uniqueness)"
        newString:
          type: string
          description: "Replacement text"
      required: ["filePath", "oldString", "newString"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "file_path": "/absolute/path/to/file.txt",
  "mode": "replace",
  "changes": {
    "replacements": 1,
    "lines_modified": [12]
  }
}
```

### 5. list_directory

**Purpose**: List directory contents with filtering.

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_list_directory"
    description: "List directory contents with optional filtering and recursion."
    parameters:
      type: object
      properties:
        dir_path:
          type: string
          description: "Path to directory"
        recursive:
          type: boolean
          default: false
          description: "Recursively list subdirectories"
        pattern:
          type: string
          description: "Glob pattern to filter files (e.g., '*.py')"
        include_hidden:
          type: boolean
          default: false
          description: "Include hidden files (starting with .)"
      required: ["dir_path"]
      additionalProperties: false
```

### 6. search_files

**Purpose**: Fast file search by filename pattern (like GitHub Copilot's `file_search`).

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_search_files"
    description: |
      Search for files by name pattern using glob syntax. Fast indexed search.
      Examples: "*.py", "**/*.test.ts", "src/**/models/*.py"
    parameters:
      type: object
      properties:
        pattern:
          type: string
          description: "Glob pattern for file search (e.g., '*.py', '**/*.test.js')"
        max_results:
          type: integer
          minimum: 1
          maximum: 500
          default: 50
          description: "Maximum number of files to return"
      required: ["pattern"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "files": [
    "/absolute/path/to/file1.py",
    "/absolute/path/to/file2.py"
  ],
  "total_found": 2,
  "truncated": false
}
```

### 7. grep_search

**Purpose**: Fast text search across files (like GitHub Copilot's `grep_search`).

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_grep_search"
    description: |
      Search for text patterns across files. Supports literal strings and regex.
      Similar to GitHub Copilot's grep_search tool - fast indexed search with context.
    parameters:
      type: object
      properties:
        query:
          type: string
          description: "Search query (literal text or regex pattern)"
        is_regex:
          type: boolean
          default: false
          description: "Treat query as regex pattern"
        include_pattern:
          type: string
          description: "Only search in files matching this glob (e.g., '**/*.py')"
        case_sensitive:
          type: boolean
          default: false
          description: "Case-sensitive search"
        max_results:
          type: integer
          minimum: 1
          maximum: 500
          default: 100
          description: "Maximum number of matches to return"
        context_lines:
          type: integer
          minimum: 0
          maximum: 10
          default: 2
          description: "Number of context lines before/after match"
      required: ["query"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "matches": [
    {
      "file_path": "/absolute/path/to/file.py",
      "line_number": 42,
      "line_content": "def search_function(query: str):",
      "context_before": [
        "class SearchEngine:",
        "    def __init__(self):"
      ],
      "context_after": [
        "    results = []",
        "    return results"
      ]
    }
  ],
  "total_matches": 1,
  "total_files": 1,
  "truncated": false
}
```

### 8. semantic_search

**Purpose**: Semantic/AI-powered code search using embeddings (ChromaDB).

**Schema**:
```yaml
- type: function
  function:
    name: "{{ name }}_semantic_search"
    description: |
      Semantic/AI-powered code search using embeddings. Finds files by meaning, not just keywords.
      Uses ChromaDB for intelligent similarity search.

      Examples:
        - "find authentication logic" → matches verify_credentials(), login(), auth functions
        - "database connection code" → finds DB setup, connection pools, ORM configs
        - "error handling patterns" → locates try/except blocks, error classes

      Better than grep for conceptual searches where exact keywords vary.
    parameters:
      type: object
      properties:
        query:
          type: string
          description: "Natural language search query describing what code you're looking for"
        max_results:
          type: integer
          minimum: 1
          maximum: 50
          default: 10
          description: "Maximum number of files to return"
        filter_pattern:
          type: string
          description: "Optional glob pattern to filter results by file type (e.g., '*.py')"
      required: ["query"]
      additionalProperties: false
```

**Response**:
```json
{
  "status": "success",
  "query": "authentication logic",
  "results": [
    {
      "file_path": "/absolute/path/to/auth.py",
      "filename": "auth.py",
      "similarity_score": 0.8934,
      "size_bytes": 4523,
      "extension": ".py"
    }
  ],
  "count": 1,
  "max_results": 10
}
```

## Security Model

### Path Validation

```python
class PathValidator:
    def validate_path(self, path: str) -> Path:
        """
        Validates a path is safe and within allowed directories.

        Security checks:
        1. Resolve to absolute path
        2. Normalize path separators
        3. Check for path traversal patterns (.., ~)
        4. Verify within allowed directories
        5. Resolve symlinks and validate targets
        6. Check for null bytes
        """
        # Convert to Path object
        p = Path(path)

        # Check for dangerous patterns
        if ".." in p.parts or "~" in str(p):
            raise SecurityError("Path traversal attempt detected")

        # Resolve to absolute path
        abs_path = p.resolve()

        # Check if within allowed directories
        if not any(abs_path.is_relative_to(allowed) for allowed in self.allowed_dirs):
            raise SecurityError(f"Path outside allowed directories: {abs_path}")

        return abs_path
```

### Configuration

Configuration in `plugins:` section:

```yaml
plugins:
  servers:
    file_operations:
      type: file_operations
      enabled: true
      config:
        allowed_directories:
          - "e:/Projects/AgentSystem/src"
          - "e:/Projects/AgentSystem/docs"
          - "e:/Projects/AgentSystem/tests"
          - "e:/Projects/AgentSystem/tmp"
        max_file_size_mb: 10
        default_encoding: "utf-8"
        
        # Read-only mode: disables all write operations
        read_only: false

        # Search configuration
        search:
          enable_indexing: true
          index_on_startup: true
          auto_reindex_interval_seconds: 300  # Rebuild index every 5 minutes
          max_file_size_for_indexing_kb: 1024  # Don't index files > 1MB
          exclude_patterns:
            - "**/.git/**"
            - "**/__pycache__/**"
            - "**/*.pyc"
            - "**/node_modules/**"
            - "**/.venv/**"
            - "**/*.min.js"
```

### Read-Only Mode

When `read_only: true` is configured:

1. **Schema Hiding**: The `file_ops_manage` and `file_ops_replace_string_in_file` tools are hidden from the schema via Jinja2 conditionals
2. **Runtime Block**: Even if tools are invoked directly, they return an error
3. **Use Case**: Safe browsing mode for code analysis agents

## Implementation Details

### Atomic File Operations

All write operations use atomic patterns:

```python
async def create_file_safe(self, path: Path, content: str) -> dict:
    """Create file atomically using temp file + rename."""
    # Write to temporary file first
    temp_path = path.with_suffix(path.suffix + ".tmp")

    try:
        async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
            await f.write(content)

        # Atomic rename (on same filesystem)
        temp_path.replace(path)

        return {"status": "success", "file_path": str(path)}
    finally:
        # Cleanup temp file if still exists
        if temp_path.exists():
            temp_path.unlink()
```

### Large File Handling

```python
async def read_file_safe(self, path: Path, offset: int = 0, limit: int = None) -> dict:
    """Read file with pagination support."""
    lines = []
    total_lines = 0

    async with aiofiles.open(path, 'r', encoding='utf-8') as f:
        async for i, line in enumerate(f):
            total_lines += 1
            if i < offset:
                continue
            if limit and len(lines) >= limit:
                break
            lines.append(line.rstrip('\n'))

    return {
        "status": "success",
        "content": '\n'.join(lines),
        "total_lines": total_lines,
        "lines_read": len(lines),
        "offset": offset
    }
```

### Background File Indexing

**Design Pattern** (like GitHub Copilot's semantic_search):

```python
class FileSearchEngine:
    """Background file indexer for fast search."""

    def __init__(self, allowed_dirs: list[Path], config: dict):
        self.allowed_dirs = allowed_dirs
        self.config = config

        # In-memory index: token -> list of (file_path, positions)
        self.text_index: dict[str, list[tuple[Path, list[int]]]] = {}
        self.file_name_index: dict[str, list[Path]] = {}
        self.file_mtimes: dict[Path, float] = {}

        # Start background indexing
        if config.get("index_on_startup", True):
            asyncio.create_task(self._background_indexer())

    async def _background_indexer(self):
        """Periodically rebuild index in background."""
        while True:
            try:
                await self.rebuild_index()

                # Wait for next index cycle
                interval = self.config.get("auto_reindex_interval_seconds", 300)
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Indexing error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Retry after 1 minute

    async def rebuild_index(self):
        """Full index rebuild."""
        logger.info("Starting file index rebuild...")
        start_time = time.time()

        new_text_index = {}
        new_file_index = {}
        files_indexed = 0

        for base_dir in self.allowed_dirs:
            async for file_path in self._iter_files(base_dir):
                # Skip excluded patterns
                if self._is_excluded(file_path):
                    continue

                # Skip large files
                if file_path.stat().st_size > self.config.get("max_file_size_for_indexing_kb", 1024) * 1024:
                    continue

                # Index filename
                new_file_index.setdefault(file_path.name.lower(), []).append(file_path)

                # Index file content (for text files only)
                if self._is_text_file(file_path):
                    await self._index_file_content(file_path, new_text_index)

                files_indexed += 1

        # Atomic swap
        self.text_index = new_text_index
        self.file_name_index = new_file_index

        elapsed = time.time() - start_time
        logger.info(f"Index rebuilt: {files_indexed} files in {elapsed:.2f}s")

    async def _index_file_content(self, file_path: Path, index: dict):
        """Index file content for grep search."""
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()

                # Tokenize and index
                # Simple word-based indexing for now
                words = re.findall(r'\w+', content.lower())
                for pos, word in enumerate(words):
                    if len(word) >= 3:  # Index words with 3+ chars
                        index.setdefault(word, []).append((file_path, pos))

        except Exception as e:
            logger.debug(f"Failed to index {file_path}: {e}")

    async def search_files(self, pattern: str, max_results: int = 50) -> list[Path]:
        """Fast file search by glob pattern."""
        import fnmatch

        results = []
        pattern_lower = pattern.lower()

        # Search in index
        for filename, paths in self.file_name_index.items():
            if fnmatch.fnmatch(filename, pattern_lower):
                results.extend(paths)
                if len(results) >= max_results:
                    break

        return results[:max_results]

    async def grep_search(
        self,
        query: str,
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
        context_lines: int = 2
    ) -> list[dict]:
        """Fast text search using index."""
        matches = []

        # For simple queries, use index for candidate files
        if not is_regex and len(query) >= 3:
            candidate_files = self._get_candidate_files_from_index(query, case_sensitive)
        else:
            # Fallback: search all indexed files
            candidate_files = list(self.file_mtimes.keys())

        # Search in candidate files
        pattern = re.compile(query, re.IGNORECASE if not case_sensitive else 0) if is_regex else None

        for file_path in candidate_files:
            try:
                async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = await f.readlines()

                for line_num, line in enumerate(lines, 1):
                    # Check match
                    if is_regex:
                        if not pattern.search(line):
                            continue
                    else:
                        check_line = line if case_sensitive else line.lower()
                        check_query = query if case_sensitive else query.lower()
                        if check_query not in check_line:
                            continue

                    # Found match - extract context
                    context_before = lines[max(0, line_num - context_lines - 1):line_num - 1]
                    context_after = lines[line_num:line_num + context_lines]

                    matches.append({
                        "file_path": str(file_path),
                        "line_number": line_num,
                        "line_content": line.rstrip('\n'),
                        "context_before": [l.rstrip('\n') for l in context_before],
                        "context_after": [l.rstrip('\n') for l in context_after]
                    })

                    if len(matches) >= max_results:
                        return matches

            except Exception as e:
                logger.debug(f"Search error in {file_path}: {e}")

        return matches

    def _get_candidate_files_from_index(self, query: str, case_sensitive: bool) -> list[Path]:
        """Get candidate files from index based on query tokens."""
        query_lower = query if case_sensitive else query.lower()
        query_tokens = re.findall(r'\w+', query_lower)

        # Find files containing query tokens
        candidate_files = set()
        for token in query_tokens:
            if token in self.text_index:
                for file_path, _ in self.text_index[token]:
                    candidate_files.add(file_path)

        return list(candidate_files)
```

**Performance Characteristics**:
- Initial indexing: ~1000 files/second
- Index rebuild: Every 5 minutes (configurable)
- Search latency: <100ms for most queries
- Memory usage: ~1MB per 1000 files indexed

### Agent Safety Pattern

**Recommended Workflow** (similar to GitHub Copilot):

```python
# 1. Read file first (keeps content in conversation history)
current = await read_file({"file_path": "src/main.py"})

# 2. Make precise edit with exact string matching
result = await edit_file({
    "file_path": "src/main.py",
    "mode": "replace",
    "old_string": "def old_function():\n    pass",  # Exact match required
    "new_string": "def new_function():\n    return True"
})

# 3. If something goes wrong, agent has original in current["content"]
```

**Safety Features**:
- **Exact String Matching**: `replace` mode requires exact match, fails if ambiguous
- **Git Integration**: Use `git diff` and `git checkout` for version control
- **Conversation History**: Agent retains original content in context
- **Atomic Operations**: File writes use temp file + rename for consistency

## Status Reporting

All tools report progress:

```python
async def read_file(self, params: dict) -> dict:
    status = params["_status"]
    file_path = params["file_path"]

    await status.progress(f"Reading file: {file_path}")

    # Validate path
    safe_path = self.validator.validate_path(file_path)

    # Read file
    result = await self.operations.read_file_safe(safe_path, ...)

    await status.complete(f"Read {result['lines_read']} lines from {file_path}")

    return result
```

## Cancellation Support

All I/O operations check for cancellation:

```python
async def read_file_safe(self, path: Path, token=None) -> dict:
    """Read with cancellation support."""
    lines = []

    async with aiofiles.open(path, 'r') as f:
        async for i, line in enumerate(f):
            # Check cancellation every N lines
            if i % 100 == 0 and token and token.is_cancelled:
                return {"status": "cancelled", "lines_read": len(lines)}

            lines.append(line)

    return {"status": "success", "content": '\n'.join(lines)}
```

## Error Handling

Comprehensive error handling with user-friendly messages:

```python
async def read_file(self, params: dict) -> dict:
    try:
        file_path = params["file_path"]
        safe_path = self.validator.validate_path(file_path)

        if not safe_path.exists():
            return {
                "status": "error",
                "error": f"File not found: {file_path}",
                "error_type": "FileNotFoundError"
            }

        if not safe_path.is_file():
            return {
                "status": "error",
                "error": f"Path is not a file: {file_path}",
                "error_type": "NotAFileError"
            }

        return await self.operations.read_file_safe(safe_path, ...)

    except SecurityError as e:
        return {"status": "error", "error": str(e), "error_type": "SecurityError"}
    except UnicodeDecodeError as e:
        return {"status": "error", "error": "File encoding error", "error_type": "EncodingError"}
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        return {"status": "error", "error": str(e), "error_type": type(e).__name__}
```

## Testing Strategy

### Unit Tests

```python
# tests/test_plugin_file_operations_basic.py

async def test_read_file_success(tmp_path):
    """Test successful file read."""
    # Create test file
    test_file = tmp_path / "test.txt"
    test_file.write_text("Line 1\nLine 2\nLine 3")

    # Initialize server with tmp_path as allowed dir
    server = FileOperationsServer("test", system_config, mcp_config)

    result = await server.read_file({
        "file_path": str(test_file),
        "_status": mock_status
    })

    assert result["status"] == "success"
    assert result["content"] == "Line 1\nLine 2\nLine 3"
    assert result["total_lines"] == 3

async def test_path_traversal_blocked(tmp_path):
    """Test path traversal attack is blocked."""
    server = FileOperationsServer("test", system_config, mcp_config)

    result = await server.read_file({
        "file_path": str(tmp_path / "../../../etc/passwd"),
        "_status": mock_status
    })

    assert result["status"] == "error"
    assert "SecurityError" in result["error_type"]
```

### Integration Tests

```python
# tests/test_plugin_file_operations_integration.py

async def test_create_edit_read_delete_workflow():
    """Test complete file lifecycle."""
    # Create file
    create_result = await server.create_file({...})
    assert create_result["status"] == "success"

    # Edit file
    edit_result = await server.edit_file({...})
    assert edit_result["status"] == "success"

    # Read file
    read_result = await server.read_file({...})
    assert "new content" in read_result["content"]

    # Delete file
    delete_result = await server.delete_file({...})
    assert delete_result["status"] == "success"
```

## CLI Support

```bash
# Read file
file-ops read --file src/main.py --offset 10 --limit 20

# Create file
file-ops create --file tmp/test.txt --content "Hello World"

# Edit file (append)
file-ops edit --file src/main.py --mode append --content "\n# New comment"

# Edit file (replace)
file-ops edit --file src/main.py --mode replace --old "old_function" --new "new_function"

# List directory
file-ops list --dir src/ --pattern "*.py" --recursive

# Delete file
file-ops delete --file tmp/test.txt --confirm
```

## Future Enhancements

1. **Binary file support**: Extend to handle binary files with base64 encoding
2. **File watching**: Monitor files for changes
3. **Diff generation**: Generate unified diffs for edits
4. **Batch operations**: Apply edits to multiple files
5. **Undo/Redo**: Maintain edit history with rollback
6. **Smart encoding detection**: Auto-detect file encoding
7. **File locking**: Prevent concurrent modifications

## References

- GitHub Copilot file editing tools (replace_string_in_file, create_file, read_file)
- Python pathlib security best practices
- MCP protocol specification
- AgentSystem plugin architecture
