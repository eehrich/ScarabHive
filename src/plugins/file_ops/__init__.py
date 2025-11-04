"""File Operations Plugin for AgentSystem.

Provides secure file manipulation tools for text files with sandboxed directory access.
Similar to GitHub Copilot's file editing capabilities with additional search/indexing.

Tools:
- read_file: Read text files with pagination support
- create_file: Create new text files
- edit_file: Edit files (append/replace/insert modes)
- delete_file: Delete files safely
- list_directory: List directory contents with filtering
- file_exists: Check file/directory existence
- get_file_info: Get file metadata
- search_files: Fast file search by glob pattern
- grep_search: Full-text search across files

Security:
- Path traversal prevention
- Sandboxed to configured directories
- Atomic file operations
- Symlink validation
"""

from __future__ import annotations

__version__ = "1.0.0"
