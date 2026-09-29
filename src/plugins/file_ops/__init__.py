"""File Operations Plugin for AgentSystem.

Provides secure file manipulation tools for text files with sandboxed directory access.
Similar to GitHub Copilot's file editing capabilities with additional search/indexing.

Tools:
- read_file: Read text files with pagination support
- manage: Create, delete, move, rename files and directories
- replace_string_in_file: Replace one exact occurrence of a string
- list_directory: List directory contents, capped, with filtering
- search_files: File search by glob pattern
- grep_search: Full-text search across files
- semantic_search: Embedding search (off unless configured)

Security:
- Path traversal prevention
- Sandboxed to configured directories
- Atomic file operations
- Symlink validation
"""

from __future__ import annotations

__version__ = "1.0.0"
