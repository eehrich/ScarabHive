"""
Tree Hierarchy Utilities for Status Messages

This module provides utilities to parse request_id patterns and build
hierarchical tree structures for status message display in the WebUI.

Request ID Pattern:
- Base ID: "abc123" (main agent)  
- First level: "abc123_001" (first tool/sub-agent)
- Second level: "abc123_001_002" (sub-tool of first tool)
- Third level: "abc123_001_002_001" (and so on...)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import re
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class TreeNode:
    """Represents a node in the hierarchical status tree."""
    
    # Core identification
    request_id: str
    parent_id: Optional[str] = None
    
    # Tree structure
    children: Dict[str, TreeNode] = field(default_factory=dict)
    depth: int = 0
    
    # Display properties
    server: str = ""
    message: str = ""
    phase: str = "progress"  # start, progress, end, error
    timestamp: str = ""
    sequence: int = 0
    
    # Tree metadata
    is_expanded: bool = True
    child_count: int = 0
    is_leaf: bool = True
    
    # Additional data
    meta: Optional[Dict[str, Any]] = None

    def add_child(self, child: TreeNode) -> None:
        """Add a child node and update tree metadata."""
        self.children[child.request_id] = child
        child.parent_id = self.request_id
        child.depth = self.depth + 1
        self.child_count = len(self.children)
        self.is_leaf = False

    def find_node(self, request_id: str) -> Optional[TreeNode]:
        """Recursively find a node by request_id."""
        if self.request_id == request_id:
            return self
        
        for child in self.children.values():
            found = child.find_node(request_id)
            if found:
                return found
        
        return None

    def get_all_descendants(self) -> List[TreeNode]:
        """Get all descendant nodes in depth-first order."""
        descendants = []
        for child in self.children.values():
            descendants.append(child)
            descendants.extend(child.get_all_descendants())
        return descendants

    def to_dict(self) -> Dict[str, Any]:
        """Convert node to dictionary for JSON serialization."""
        return {
            "request_id": self.request_id,
            "parent_id": self.parent_id,
            "server": self.server,
            "message": self.message,
            "phase": self.phase,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "depth": self.depth,
            "is_expanded": self.is_expanded,
            "child_count": self.child_count,
            "is_leaf": self.is_leaf,
            "children": {rid: child.to_dict() for rid, child in self.children.items()},
            "meta": self.meta
        }


class StatusTreeBuilder:
    """Builds hierarchical trees from status events with request_id patterns.
    
    Memory management:
    - Trees are automatically cleaned up after max_age_seconds (default: 1 hour)
    - Maximum number of trees is limited by max_trees (default: 500)
    - Cleanup runs automatically on add_status_event() calls
    """
    
    # Hierarchy suffixes, matched at the RIGHT edge only: "_nnn" (internal
    # tool requests) or the sub_agent_manager forms "_sub_<id>",
    # "_sub_cont_<id>" (continue), "_async_<id>" and "_minlen_<n>" (retry).
    # Matching "_nnn" anywhere in the id used to mangle "abc_001_sub_x" into
    # "abc_sub_x" and invent parent ids that never existed.
    SUFFIX_PATTERN = re.compile(
        r'_((?:sub_cont|sub|async|minlen)_[0-9a-zA-Z]+|\d{3})$')
    
    def __init__(self, max_trees: int = 500, max_age_seconds: float = 3600.0):
        """Initialize the tree builder with memory limits.
        
        Args:
            max_trees: Maximum number of trees to keep (LRU eviction when exceeded)
            max_age_seconds: Maximum age of trees in seconds (default: 1 hour)
        """
        self.trees: Dict[str, TreeNode] = {}  # base_id -> root_node
        self.node_index: Dict[str, TreeNode] = {}  # request_id -> node (for fast lookup)
        self._timestamps: Dict[str, float] = {}  # base_id -> creation_time
        self._max_trees = max_trees
        self._max_age = max_age_seconds
        self._last_cleanup = time.time()
        self._cleanup_interval = 60.0  # Run cleanup at most every 60 seconds
    
    def parse_request_id(self, request_id: str) -> tuple[str, List[str]]:
        """
        Parse request_id into base_id and suffix parts.
        
        Examples:
        - "abc123" -> ("abc123", [])
        - "abc123_001" -> ("abc123", ["001"])  
        - "abc123_001_002" -> ("abc123", ["001", "002"])
        
        Returns:
            tuple of (base_id, suffix_list)
        """
        if not request_id:
            return "", []

        # Peel suffixes off the right edge until none is left
        suffixes: List[str] = []
        base_id = request_id
        while True:
            m = self.SUFFIX_PATTERN.search(base_id)
            if not m or m.start() == 0:
                break
            suffixes.insert(0, m.group(1))
            base_id = base_id[:m.start()]

        return base_id, suffixes

    def get_parent_id(self, request_id: str) -> Optional[str]:
        """
        Get the parent request_id for a given request_id.
        
        Examples:
        - "abc123" -> None (root)
        - "abc123_001" -> "abc123"  
        - "abc123_001_002" -> "abc123_001"
        """
        base_id, suffixes = self.parse_request_id(request_id)
        
        if not suffixes:
            return None  # Root node
        
        if len(suffixes) == 1:
            return base_id  # Parent is the base
        
        # Parent is base + all suffixes except the last one
        parent_suffixes = suffixes[:-1]
        parent_id = base_id
        for suffix in parent_suffixes:
            parent_id += f"_{suffix}"
        
        return parent_id

    def add_status_event(self, request_id: str, server: str = "", message: str = "", 
                        phase: str = "progress", timestamp: str = "", 
                        sequence: int = 0, meta: Optional[Dict[str, Any]] = None) -> TreeNode:
        """
        Add a status event to the tree structure.
        
        Creates nodes as needed and maintains parent-child relationships.
        Automatically cleans up old trees to prevent memory leaks.
        """
        # Periodic cleanup to prevent memory leaks
        self._maybe_cleanup()
        
        if not request_id:
            logger.warning("Empty request_id provided to tree builder")
            return TreeNode(request_id="unknown", server=server, message=message)
        
        # Check if node already exists
        if request_id in self.node_index:
            node = self.node_index[request_id]
            # Update existing node with new status
            if message:
                node.message = message
            if phase:
                node.phase = phase
            if timestamp:
                node.timestamp = timestamp
            if sequence:
                node.sequence = sequence
            if meta:
                node.meta = meta
            
            # Update tree timestamp on any activity (keeps tree alive)
            base_id, _ = self.parse_request_id(request_id)
            if base_id in self._timestamps:
                self._timestamps[base_id] = time.time()
            
            return node
        
        # Create new node
        base_id, suffixes = self.parse_request_id(request_id)
        depth = len(suffixes)
        
        node = TreeNode(
            request_id=request_id,
            server=server,
            message=message,
            phase=phase,
            timestamp=timestamp,
            sequence=sequence,
            depth=depth,
            meta=meta
        )
        
        # Add to index
        self.node_index[request_id] = node
        
        # Handle root node
        if depth == 0:
            self.trees[base_id] = node
            self._timestamps[base_id] = time.time()  # Track creation time
            return node
        
        # Find or create parent node
        parent_id = self.get_parent_id(request_id)
        if parent_id:
            parent_node = self._ensure_node_exists(parent_id)
            parent_node.add_child(node)
        else:
            # Orphaned node - create as separate tree
            logger.warning(f"Orphaned node {request_id} - creating separate tree")
            self.trees[request_id] = node
            self._timestamps[request_id] = time.time()  # Track creation time
        
        return node
    
    def _maybe_cleanup(self) -> None:
        """Run cleanup if enough time has passed since last cleanup."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        self._last_cleanup = now
        self._cleanup_old_trees()
    
    def _cleanup_old_trees(self) -> None:
        """Remove old trees to prevent memory leak.
        
        Only removes trees that are:
        1. Completed (root node has phase 'end' or 'error') AND older than 5 minutes
        2. OR older than max_age (regardless of completion status - fallback for orphaned trees)
        """
        now = time.time()
        completed_min_age = 300.0  # 5 minutes - keep completed trees briefly for late events
        
        to_remove = []
        for base_id, ts in self._timestamps.items():
            age = now - ts
            tree = self.trees.get(base_id)
            
            if tree is None:
                # Orphaned timestamp entry
                to_remove.append(base_id)
                continue
            
            # Check if tree is completed (root node has end/error phase)
            is_completed = tree.phase in ("end", "error")
            
            if is_completed and age > completed_min_age:
                # Completed tree older than 5 minutes - safe to remove
                to_remove.append(base_id)
            elif age > self._max_age:
                # Very old tree (>1h) - remove regardless of status (orphaned/stuck)
                to_remove.append(base_id)
        
        for base_id in to_remove:
            self._remove_tree(base_id)
        
        if to_remove:
            logger.debug(f"StatusTreeBuilder: Cleaned up {len(to_remove)} trees")
        
        # LRU eviction if still over limit (only remove completed trees first)
        evicted = 0
        while len(self.trees) > self._max_trees:
            if not self._timestamps:
                break
            
            # Prefer removing completed trees first
            completed_trees = [
                bid for bid in self._timestamps
                if self.trees.get(bid) and self.trees[bid].phase in ("end", "error")
            ]
            
            if completed_trees:
                # Remove oldest completed tree
                oldest = min(completed_trees, key=lambda k: self._timestamps.get(k, 0))
            else:
                # No completed trees - remove oldest overall (shouldn't happen often)
                oldest = min(self._timestamps, key=self._timestamps.get)  # type: ignore[arg-type]
            
            self._remove_tree(oldest)
            evicted += 1
        
        if evicted:
            logger.debug(f"StatusTreeBuilder: Evicted {evicted} trees (LRU, max={self._max_trees})")
    
    def _remove_tree(self, base_id: str) -> None:
        """Remove a tree and all its nodes from the index."""
        if base_id not in self.trees:
            return
        
        tree = self.trees[base_id]
        
        # Remove all nodes in this tree from index
        nodes_to_remove = [tree.request_id]
        for node in tree.get_all_descendants():
            nodes_to_remove.append(node.request_id)
        
        for rid in nodes_to_remove:
            self.node_index.pop(rid, None)
        
        # Remove tree and timestamp
        del self.trees[base_id]
        self._timestamps.pop(base_id, None)

    def _ensure_node_exists(self, request_id: str) -> TreeNode:
        """Ensure a node exists, creating it and its ancestors if needed."""
        if request_id in self.node_index:
            return self.node_index[request_id]
        
        # Create the node (this will recursively create parents)
        return self.add_status_event(request_id, server="", message="", phase="progress")

    def get_tree(self, base_id: str) -> Optional[TreeNode]:
        """Get the root node of a tree by base_id."""
        return self.trees.get(base_id)

    def get_node(self, request_id: str) -> Optional[TreeNode]:
        """Get a specific node by request_id."""
        return self.node_index.get(request_id)

    def get_all_trees(self) -> Dict[str, TreeNode]:
        """Get all trees indexed by base_id."""
        return self.trees.copy()

    def to_dict(self) -> Dict[str, Any]:
        """Convert all trees to dictionary format for JSON serialization."""
        return {
            "trees": {base_id: tree.to_dict() for base_id, tree in self.trees.items()},
            "node_count": len(self.node_index),
            "tree_count": len(self.trees),
            "max_trees": self._max_trees,
            "max_age_seconds": self._max_age
        }

    def clear(self) -> None:
        """Clear all trees and reset the builder."""
        self.trees.clear()
        self.node_index.clear()
        self._timestamps.clear()
    
    def remove_tree(self, base_id: str) -> bool:
        """Manually remove a specific tree.
        
        Args:
            base_id: The base request ID of the tree to remove
            
        Returns:
            True if tree was removed, False if it didn't exist
        """
        if base_id not in self.trees:
            return False
        self._remove_tree(base_id)
        return True
    
    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics for monitoring."""
        return {
            "tree_count": len(self.trees),
            "node_count": len(self.node_index),
            "max_trees": self._max_trees,
            "max_age_seconds": self._max_age,
            "oldest_tree_age": (
                time.time() - min(self._timestamps.values()) 
                if self._timestamps else 0
            )
        }


# Global instance for the application
_global_tree_builder = StatusTreeBuilder()


def get_tree_builder() -> StatusTreeBuilder:
    """Get the global tree builder instance."""
    return _global_tree_builder


def parse_request_id_hierarchy(request_id: str) -> Dict[str, Any]:
    """
    Convenience function to parse request_id and return hierarchy info.
    
    Returns:
        Dict with keys: base_id, suffixes, depth, parent_id, is_root
    """
    builder = StatusTreeBuilder()
    base_id, suffixes = builder.parse_request_id(request_id)
    parent_id = builder.get_parent_id(request_id)
    
    return {
        "base_id": base_id,
        "suffixes": suffixes,
        "depth": len(suffixes),
        "parent_id": parent_id,
        "is_root": parent_id is None
    }