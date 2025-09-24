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
    """Builds hierarchical trees from status events with request_id patterns."""
    
    # Regex to parse request_id suffixes: base_id_nnn_nnn_...
    REQUEST_ID_PATTERN = re.compile(r'^([^_]+)(?:_(\d{3}))*$')
    SUFFIX_PATTERN = re.compile(r'_(\d{3})')
    
    def __init__(self):
        self.trees: Dict[str, TreeNode] = {}  # base_id -> root_node
        self.node_index: Dict[str, TreeNode] = {}  # request_id -> node (for fast lookup)
    
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
        
        # Find all _nnn suffixes
        suffixes = self.SUFFIX_PATTERN.findall(request_id)
        
        # Extract base_id by removing all _nnn suffixes
        base_id = request_id
        for suffix in suffixes:
            base_id = base_id.replace(f"_{suffix}", "", 1)
        
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
        """
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
        
        return node

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
            "tree_count": len(self.trees)
        }

    def clear(self) -> None:
        """Clear all trees and reset the builder."""
        self.trees.clear()
        self.node_index.clear()


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