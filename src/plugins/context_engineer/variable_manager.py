"""Variable Manager - Variable substitution for large content blocks.

This module provides variable substitution for large content blocks like
code files, documents, or tool outputs. Large content is replaced with
$VAR_N references, and the full content can be retrieved on demand.

Key features:
- Automatic variable creation for large content
- Content type detection (code, text, json, etc.)
- Summary generation for each variable
- Session-scoped variable namespaces
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_system.llm.token_utils import estimate_content_tokens

logger = logging.getLogger(__name__)


@dataclass
class VariableEntry:
    """A stored variable entry."""
    name: str  # e.g., $VAR_1
    content: str
    content_type: str  # code, text, json, file, etc.
    token_count: int
    summary: str
    created_at: datetime = field(default_factory=datetime.now)
    source: str | None = None  # Where the content came from (e.g., file path)
    access_count: int = 0
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "content": self.content,
            "content_type": self.content_type,
            "token_count": self.token_count,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "access_count": self.access_count
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VariableEntry":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            content=data["content"],
            content_type=data["content_type"],
            token_count=data["token_count"],
            summary=data["summary"],
            created_at=datetime.fromisoformat(data["created_at"]),
            source=data.get("source"),
            access_count=data.get("access_count", 0)
        )


class VariableManager:
    """Manages variable substitution for large content blocks.
    
    When the LLM encounters large content blocks (code files, documents, etc.),
    they can be stored as variables ($VAR_1, $VAR_2, etc.) to reduce token usage.
    The LLM can then reference these variables by name without repeating the
    full content.
    
    Usage:
        vm = VariableManager()
        
        # Create a variable for large code
        var_name, summary = vm.create_variable(
            content="def process_data():\\n    # ... 500 lines ...",
            content_type="code",
            source="main.py"
        )
        # Returns: ("$VAR_1", "[Python code: function process_data, ~500 lines]")
        
        # Get summary for system prompt
        prompt_section = vm.to_system_prompt_section()
        
        # Retrieve full content later
        entry = vm.get_variable("$VAR_1")
        print(entry.content)
    """
    
    CONTENT_TYPES = ["code", "text", "json", "xml", "file", "output", "data"]
    
    def __init__(
        self,
        min_content_tokens: int = 500,
        storage_path: Path | None = None,
        session_id: str | None = None
    ):
        """Initialize variable manager.
        
        Args:
            min_content_tokens: Minimum tokens to trigger variable creation
            storage_path: Path to persist variables (optional)
            session_id: Session ID for isolation
        """
        self.min_content_tokens = min_content_tokens
        self.storage_path = storage_path
        self.session_id = session_id
        
        self.variables: dict[str, VariableEntry] = {}
        self._var_counter = 0
        
        # Load from storage if exists
        if storage_path and storage_path.exists():
            self._load()
    
    async def create_variable(
        self,
        content: str,
        content_type: str = "text",
        source: str | None = None,
        force: bool = False
    ) -> tuple[str, str]:
        """Create a variable for content.
        
        Args:
            content: The content to store
            content_type: Type of content (code, text, json, etc.)
            source: Optional source identifier (e.g., file path)
            force: Create even if below token threshold
            
        Returns:
            Tuple of (variable_name, summary)
        """
        # Check token count
        token_count = estimate_content_tokens(content)
        
        if not force and token_count < self.min_content_tokens:
            logger.debug(
                f"Content too small for variable ({token_count} < {self.min_content_tokens} tokens)"
            )
            return ("", content)  # Return original content as "summary"
        
        # Check for duplicate content
        content_hash = hash(content)
        for var_name, entry in self.variables.items():
            if hash(entry.content) == content_hash and entry.content == content:
                logger.debug(f"Found existing variable for identical content: {var_name}")
                entry.access_count += 1
                return (var_name, entry.summary)
        
        # Validate content type
        if content_type not in self.CONTENT_TYPES:
            content_type = self._detect_content_type(content)
        
        # Generate variable name
        self._var_counter += 1
        var_name = f"$VAR_{self._var_counter}"
        
        # Generate summary
        summary = self._generate_summary(content, content_type, source)
        
        # Create entry
        entry = VariableEntry(
            name=var_name,
            content=content,
            content_type=content_type,
            token_count=token_count,
            summary=summary,
            source=source
        )
        
        self.variables[var_name] = entry
        
        logger.debug(
            f"Created variable {var_name}: type={content_type}, "
            f"tokens={token_count}, summary='{summary[:50]}...'"
        )
        
        # Persist if configured
        if self.storage_path:
            await self._save()
        
        return (var_name, summary)
    
    def get_variable(self, var_name: str) -> VariableEntry | None:
        """Get a variable by name.
        
        Args:
            var_name: Variable name (e.g., "$VAR_1")
            
        Returns:
            VariableEntry if found, None otherwise
        """
        # Normalize name
        if not var_name.startswith("$"):
            var_name = f"${var_name}"
        
        entry = self.variables.get(var_name)
        if entry:
            entry.access_count += 1
        
        return entry
    
    def expand_variables(self, text: str) -> str:
        """Expand all variable references in text.
        
        Args:
            text: Text containing $VAR_N references
            
        Returns:
            Text with variables expanded to full content
        """
        for var_name, entry in self.variables.items():
            text = text.replace(var_name, entry.content)
            entry.access_count += 1
        
        return text
    
    def find_variables(self, text: str) -> list[str]:
        """Find all variable references in text.
        
        Args:
            text: Text to search
            
        Returns:
            List of variable names found
        """
        pattern = r'\$VAR_\d+'
        return re.findall(pattern, text)
    
    def to_system_prompt_section(self) -> str:
        """Format variables as a system prompt section.
        
        Returns:
            Formatted string for injection into system prompt
        """
        if not self.variables:
            return ""
        
        lines = ["<stored_variables>"]
        lines.append("The following large content blocks are stored as variables.")
        lines.append("Reference them by name. Use get_variable tool to retrieve full content.")
        lines.append("")
        
        for var_name, entry in sorted(self.variables.items()):
            lines.append(f"- {var_name}: {entry.summary}")
        
        lines.append("</stored_variables>")
        
        return "\n".join(lines)
    
    async def cleanup_unused_variables(self, messages: list[dict[str, Any]]) -> int:
        """Remove variables that are no longer referenced in message history.
        
        Args:
            messages: Current message history to scan for references
            
        Returns:
            Number of variables removed
        """
        # Collect all variable references in messages
        referenced_vars = set()
        var_pattern = r'\$VAR_\d+'
        
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = re.findall(var_pattern, content)
                referenced_vars.update(matches)
        
        # Find unreferenced variables
        all_vars = set(self.variables.keys())
        unreferenced = all_vars - referenced_vars
        
        # Remove unreferenced variables
        removed_count = 0
        for var_name in unreferenced:
            if var_name in self.variables:
                del self.variables[var_name]
                removed_count += 1
                logger.debug(f"Removed unreferenced variable: {var_name}")
        
        # Persist changes if any variables were removed
        if removed_count > 0:
            await self._save()
            logger.info(f"Cleaned up {removed_count} unreferenced variables")
        
        return removed_count
    
    def get_stats(self) -> dict[str, Any]:
        """Get statistics about stored variables.
        
        Returns:
            Dictionary with stats including variable details
        """
        total_tokens = sum(e.token_count for e in self.variables.values())
        by_type = {}
        for entry in self.variables.values():
            by_type[entry.content_type] = by_type.get(entry.content_type, 0) + 1
        
        # Include variable details for UI display
        variable_details = []
        for name, entry in self.variables.items():
            # Generate preview (first 100 chars)
            preview = entry.content[:100] + "..." if len(entry.content) > 100 else entry.content
            variable_details.append({
                "name": name,
                "content_type": entry.content_type,
                "tokens": entry.token_count,
                "preview": preview
            })
        
        return {
            "total_variables": len(self.variables),
            "total_tokens": total_tokens,  # Kept for compatibility
            "total_tokens_stored": total_tokens,
            "by_type": by_type,
            "variable_names": list(self.variables.keys()),
            "variables": variable_details  # Detailed list for UI
        }
    
    async def clear(self) -> int:
        """Clear all variables.
        
        Returns:
            Number of variables cleared
        """
        count = len(self.variables)
        self.variables = {}
        self._var_counter = 0
        
        if self.storage_path:
            await self._save()
        
        return count
    
    def _detect_content_type(self, content: str) -> str:
        """Detect content type from content.
        
        Args:
            content: The content to analyze
            
        Returns:
            Detected content type
        """
        content_start = content.strip()[:500]
        
        # JSON detection
        if content_start.startswith(("{", "[")):
            try:
                json.loads(content)
                return "json"
            except Exception:
                pass
        
        # XML/HTML detection
        if content_start.startswith("<") and ">" in content_start:
            return "xml"
        
        # Code detection (simple heuristics)
        code_patterns = [
            r'^\s*(def |class |import |from |function |const |let |var |public |private )',
            r'^\s*(if\s*\(|for\s*\(|while\s*\(|switch\s*\()',
            r'^\s*#include|^\s*#define',
        ]
        for pattern in code_patterns:
            if re.search(pattern, content, re.MULTILINE):
                return "code"
        
        return "text"
    
    def _generate_summary(
        self,
        content: str,
        content_type: str,
        source: str | None
    ) -> str:
        """Generate a brief summary for the content.
        
        Args:
            content: The content
            content_type: Type of content
            source: Optional source identifier
            
        Returns:
            Brief summary string
        """
        lines = content.count('\n') + 1
        chars = len(content)
        
        if content_type == "code":
            # Try to extract code structure
            summary_parts = []
            
            # Python functions/classes
            func_matches = re.findall(r'^(?:async\s+)?def\s+(\w+)', content, re.MULTILINE)
            class_matches = re.findall(r'^class\s+(\w+)', content, re.MULTILINE)
            
            if class_matches:
                summary_parts.append(f"classes: {', '.join(class_matches[:3])}")
            if func_matches:
                funcs = func_matches[:5]
                if len(func_matches) > 5:
                    funcs.append(f"+{len(func_matches)-5} more")
                summary_parts.append(f"functions: {', '.join(funcs)}")
            
            if summary_parts:
                structure = "; ".join(summary_parts)
                base = f"[Code: {structure}, {lines} lines]"
            else:
                base = f"[Code: {lines} lines, {chars} chars]"
        
        elif content_type == "json":
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    keys = list(data.keys())[:5]
                    if len(data) > 5:
                        keys.append(f"+{len(data)-5} more keys")
                    base = f"[JSON object: {', '.join(keys)}]"
                elif isinstance(data, list):
                    base = f"[JSON array: {len(data)} items]"
                else:
                    base = f"[JSON: {type(data).__name__}]"
            except Exception:
                base = f"[JSON-like: {lines} lines]"
        
        elif content_type == "xml":
            # Extract root element
            root_match = re.search(r'<(\w+)', content)
            root = root_match.group(1) if root_match else "unknown"
            base = f"[XML: root=<{root}>, {lines} lines]"
        
        else:
            # Text - extract first sentence or line
            first_line = content.split('\n')[0][:100]
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            base = f"[Text: '{first_line}', {lines} lines]"
        
        # Add source if available
        if source:
            base = f"{base} (from: {source})"
        
        return base
    
    def _save_sync(self) -> None:
        """Save to storage path - sync version for thread pool."""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "session_id": self.session_id,
            "var_counter": self._var_counter,
            "variables": {k: v.to_dict() for k, v in self.variables.items()}
        }
        
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    
    async def _save(self) -> None:
        """Save to storage path - async wrapper."""
        if not self.storage_path:
            return
        await asyncio.to_thread(self._save_sync)
    
    def _load(self) -> None:
        """Load from storage path."""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            self._var_counter = data.get("var_counter", 0)
            self.variables = {
                k: VariableEntry.from_dict(v)
                for k, v in data.get("variables", {}).items()
            }
            
            logger.debug(
                f"Loaded {len(self.variables)} variables, counter={self._var_counter}"
            )
        except Exception as e:
            logger.error(f"Failed to load variables from {self.storage_path}: {e}")
            self.variables = {}
            self._var_counter = 0
