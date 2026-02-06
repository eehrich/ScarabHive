"""
Tests for tree hierarchy utilities.
"""

# Test cases for tree hierarchy
from agent_system.utils.tree_hierarchy import (
    TreeNode, StatusTreeBuilder, parse_request_id_hierarchy, get_tree_builder
)


class TestTreeNode:
    """Test TreeNode functionality."""
    
    def test_create_node(self):
        """Test basic node creation."""
        node = TreeNode(
            request_id="test_001",
            server="test_server", 
            message="test message"
        )
        
        assert node.request_id == "test_001"
        assert node.server == "test_server"
        assert node.message == "test message"
        assert node.depth == 0
        assert node.is_leaf is True
        assert node.child_count == 0

    def test_add_child(self):
        """Test adding child nodes."""
        parent = TreeNode(request_id="parent")
        child1 = TreeNode(request_id="parent_001")
        child2 = TreeNode(request_id="parent_002")
        
        parent.add_child(child1)
        parent.add_child(child2)
        
        assert len(parent.children) == 2
        assert child1.parent_id == "parent"
        assert child2.parent_id == "parent"
        assert child1.depth == 1
        assert child2.depth == 1
        assert parent.is_leaf is False
        assert parent.child_count == 2

    def test_find_node(self):
        """Test node finding by request_id."""
        root = TreeNode(request_id="root")
        child = TreeNode(request_id="root_001")
        grandchild = TreeNode(request_id="root_001_001")
        
        root.add_child(child)
        child.add_child(grandchild)
        
        assert root.find_node("root") is root
        assert root.find_node("root_001") is child
        assert root.find_node("root_001_001") is grandchild
        assert root.find_node("nonexistent") is None

    def test_get_all_descendants(self):
        """Test getting all descendants."""
        root = TreeNode(request_id="root")
        child1 = TreeNode(request_id="root_001") 
        child2 = TreeNode(request_id="root_002")
        grandchild = TreeNode(request_id="root_001_001")
        
        root.add_child(child1)
        root.add_child(child2)
        child1.add_child(grandchild)
        
        descendants = root.get_all_descendants()
        request_ids = [node.request_id for node in descendants]
        
        assert len(descendants) == 3
        assert "root_001" in request_ids
        assert "root_002" in request_ids
        assert "root_001_001" in request_ids

    def test_to_dict(self):
        """Test serialization to dictionary."""
        node = TreeNode(
            request_id="test_001",
            server="test_server",
            message="test message",
            meta={"key": "value"}
        )
        
        data = node.to_dict()
        
        assert data["request_id"] == "test_001"
        assert data["server"] == "test_server"
        assert data["message"] == "test message"
        assert data["meta"]["key"] == "value"
        assert data["depth"] == 0
        assert data["is_leaf"] is True


class TestStatusTreeBuilder:
    """Test StatusTreeBuilder functionality."""
    
    def test_parse_request_id(self):
        """Test request_id parsing."""
        builder = StatusTreeBuilder()
        
        # Root ID
        base_id, suffixes = builder.parse_request_id("abc123")
        assert base_id == "abc123"
        assert suffixes == []
        
        # Single suffix
        base_id, suffixes = builder.parse_request_id("abc123_001")
        assert base_id == "abc123"
        assert suffixes == ["001"]
        
        # Multiple suffixes
        base_id, suffixes = builder.parse_request_id("abc123_001_002")
        assert base_id == "abc123"
        assert suffixes == ["001", "002"]
        
        # Complex base ID
        base_id, suffixes = builder.parse_request_id("agent_web_001_002")
        assert base_id == "agent_web"
        assert suffixes == ["001", "002"]

    def test_get_parent_id(self):
        """Test parent ID calculation."""
        builder = StatusTreeBuilder()
        
        # Root has no parent
        assert builder.get_parent_id("abc123") is None
        
        # First level parent is base
        assert builder.get_parent_id("abc123_001") == "abc123"
        
        # Nested levels
        assert builder.get_parent_id("abc123_001_002") == "abc123_001"
        assert builder.get_parent_id("abc123_001_002_003") == "abc123_001_002"

    def test_add_status_event_root(self):
        """Test adding root status event."""
        builder = StatusTreeBuilder()
        
        node = builder.add_status_event(
            request_id="abc123",
            server="main_agent",
            message="Starting task"
        )
        
        assert node.request_id == "abc123"
        assert node.depth == 0
        assert node.parent_id is None
        assert "abc123" in builder.trees
        assert builder.get_node("abc123") is node

    def test_add_status_event_child(self):
        """Test adding child status events."""
        builder = StatusTreeBuilder()
        
        # Add root first
        root = builder.add_status_event("abc123", "main_agent", "Starting")
        
        # Add child
        child = builder.add_status_event("abc123_001", "tool_executor", "Running tool")
        
        assert child.depth == 1
        assert child.parent_id == "abc123"
        assert child in root.children.values()
        assert root.child_count == 1
        assert root.is_leaf is False

    def test_add_status_event_deep_hierarchy(self):
        """Test adding deeply nested status events."""
        builder = StatusTreeBuilder()
        
        # Build hierarchy: root -> child -> grandchild -> great-grandchild
        root = builder.add_status_event("base", "main", "Root")
        child = builder.add_status_event("base_001", "tool1", "Child")  
        grandchild = builder.add_status_event("base_001_002", "tool2", "Grandchild")
        great_grandchild = builder.add_status_event("base_001_002_003", "tool3", "Great-grandchild")
        
        # Verify structure
        assert root.depth == 0
        assert child.depth == 1
        assert grandchild.depth == 2
        assert great_grandchild.depth == 3
        
        # Verify relationships
        assert child.parent_id == "base"
        assert grandchild.parent_id == "base_001"
        assert great_grandchild.parent_id == "base_001_002"
        
        # Verify tree navigation
        found_grandchild = root.find_node("base_001_002")
        assert found_grandchild is grandchild

    def test_update_existing_node(self):
        """Test updating existing node status.""" 
        builder = StatusTreeBuilder()
        
        # Add initial status
        node1 = builder.add_status_event("abc123", "agent", "Starting", "start")
        
        # Update status
        node2 = builder.add_status_event("abc123", "agent", "Completed", "end") 
        
        # Should be same node, updated
        assert node1 is node2
        assert node1.message == "Completed"
        assert node1.phase == "end"

    def test_orphaned_node(self):
        """Test handling orphaned nodes (missing parent)."""
        builder = StatusTreeBuilder()
        
        # Add child without parent first - this should auto-create the parent
        child = builder.add_status_event("abc123_001", "orphan", "Child without parent")
        
        # Should auto-create parent node and establish proper hierarchy
        assert len(builder.trees) == 1
        assert "abc123" in builder.trees  # Parent becomes root
        assert child.parent_id == "abc123"
        
        # Parent should exist and have the child
        parent = builder.get_node("abc123")
        assert parent is not None
        assert child in parent.children.values()
        
        # Now add explicit status to the parent
        updated_parent = builder.add_status_event("abc123", "parent", "Parent message")
        
        # Should be same node, just updated
        assert updated_parent is parent
        assert updated_parent.message == "Parent message"
        assert child in updated_parent.children.values()

    def test_multiple_trees(self):
        """Test multiple independent trees.""" 
        builder = StatusTreeBuilder()
        
        # Add nodes for two different base IDs
        tree1_root = builder.add_status_event("request1", "agent1", "Task 1")
        tree1_child = builder.add_status_event("request1_001", "tool1", "Tool 1")
        
        tree2_root = builder.add_status_event("request2", "agent2", "Task 2") 
        tree2_child = builder.add_status_event("request2_001", "tool2", "Tool 2")
        
        # Should have two separate trees
        assert len(builder.trees) == 2
        assert "request1" in builder.trees
        assert "request2" in builder.trees
        
        # Trees should be independent
        assert tree1_child not in tree2_root.children.values()
        assert tree2_child not in tree1_root.children.values()

    def test_to_dict(self):
        """Test tree serialization."""
        builder = StatusTreeBuilder()
        
        builder.add_status_event("root", "main", "Root message")
        builder.add_status_event("root_001", "tool", "Tool message")
        
        data = builder.to_dict()
        
        assert "trees" in data
        assert "root" in data["trees"]
        assert data["node_count"] == 2
        assert data["tree_count"] == 1
        
        # Check nested structure
        tree_data = data["trees"]["root"]
        assert tree_data["request_id"] == "root"
        assert tree_data["message"] == "Root message"
        assert len(tree_data["children"]) == 1

    def test_clear(self):
        """Test clearing all trees."""
        builder = StatusTreeBuilder()
        
        builder.add_status_event("root", "main", "Root")
        builder.add_status_event("root_001", "tool", "Tool")
        
        assert len(builder.trees) == 1
        assert len(builder.node_index) == 2
        
        builder.clear()
        
        assert len(builder.trees) == 0
        assert len(builder.node_index) == 0


class TestUtilityFunctions:
    """Test utility functions."""
    
    def test_parse_request_id_hierarchy(self):
        """Test convenience function for parsing request ID hierarchy."""
        
        # Root level
        info = parse_request_id_hierarchy("abc123")
        assert info["base_id"] == "abc123"
        assert info["suffixes"] == []
        assert info["depth"] == 0
        assert info["parent_id"] is None
        assert info["is_root"] is True
        
        # Child level  
        info = parse_request_id_hierarchy("abc123_001")
        assert info["base_id"] == "abc123"
        assert info["suffixes"] == ["001"]
        assert info["depth"] == 1
        assert info["parent_id"] == "abc123"
        assert info["is_root"] is False
        
        # Grandchild level
        info = parse_request_id_hierarchy("abc123_001_002")
        assert info["base_id"] == "abc123"
        assert info["suffixes"] == ["001", "002"]
        assert info["depth"] == 2  
        assert info["parent_id"] == "abc123_001"
        assert info["is_root"] is False

    def test_global_tree_builder(self):
        """Test global tree builder instance."""
        builder1 = get_tree_builder()
        builder2 = get_tree_builder()
        
        # Should be same instance
        assert builder1 is builder2
        
        # Test that it works as expected
        builder1.add_status_event("test", "server", "message")
        assert builder2.get_node("test") is not None


class TestEdgeCases:
    """Test edge cases and error conditions."""
    
    def test_empty_request_id(self):
        """Test handling empty request_id."""
        builder = StatusTreeBuilder()
        
        node = builder.add_status_event("", "server", "message")
        assert node.request_id == "unknown"

    def test_malformed_request_id(self):
        """Test handling malformed request IDs."""
        builder = StatusTreeBuilder()
        
        # Non-standard suffix format (should still work)
        node1 = builder.add_status_event("abc_123", "server", "message")
        assert node1.request_id == "abc_123"
        
        # Mixed formats
        node2 = builder.add_status_event("abc_123_001", "server", "message") 
        assert node2.request_id == "abc_123_001"

    def test_very_deep_hierarchy(self):
        """Test very deep request ID hierarchy."""
        builder = StatusTreeBuilder()
        
        # Build 10-level deep hierarchy
        request_id = "root"
        for i in range(1, 11):
            request_id += f"_{i:03d}"
            node = builder.add_status_event(request_id, f"server_{i}", f"Message {i}")
            assert node.depth == i

    def test_concurrent_modifications(self):
        """Test adding nodes in non-sequential order."""
        builder = StatusTreeBuilder()
        
        # Add nodes out of order
        builder.add_status_event("base_001_002", "server3", "Deep child first")
        builder.add_status_event("base", "server1", "Root second")
        builder.add_status_event("base_001", "server2", "Middle third")
        
        # Should still build correct hierarchy
        root = builder.get_node("base")
        child = builder.get_node("base_001")
        grandchild = builder.get_node("base_001_002")
        
        assert grandchild.parent_id == "base_001"
        assert child.parent_id == "base" 
        assert grandchild in child.children.values()
        assert child in root.children.values()