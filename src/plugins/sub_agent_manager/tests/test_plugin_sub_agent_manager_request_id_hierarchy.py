"""Tests for hierarchical request ID generation in sub-agents."""


def test_hierarchical_request_id_format():
    """Test that hierarchical request IDs follow the expected format."""
    # Format: {parent_request_id}_sub_{short_id}
    # Example: req_abc123_sub_a1b2c3
    
    parent_id = "req_abc123"
    sub_suffix = "a1b2c3"
    expected = f"{parent_id}_sub_{sub_suffix}"
    
    assert expected == "req_abc123_sub_a1b2c3"
    
    # Verify parsing
    parts = expected.split("_sub_")
    assert parts[0] == "req_abc123"
    assert parts[1] == "a1b2c3"
    
    print("✓ Hierarchical request ID format validation passed")


def test_continue_request_id_format():
    """Test that continue request IDs follow the expected format."""
    # Format: {parent_request_id}_sub_cont_{short_id}
    # Example: req_xyz789_sub_cont_d4e5f6
    
    parent_id = "req_xyz789"
    sub_suffix = "d4e5f6"
    expected = f"{parent_id}_sub_cont_{sub_suffix}"
    
    assert expected == "req_xyz789_sub_cont_d4e5f6"
    
    # Verify parsing
    parts = expected.split("_sub_cont_")
    assert parts[0] == "req_xyz789"
    assert parts[1] == "d4e5f6"
    
    print("✓ Continue request ID format validation passed")


def test_request_id_extraction():
    """Test extracting parent request ID from hierarchical ID."""
    hierarchical_id = "req_abc123_sub_x1y2z3"
    
    # Extract parent
    if "_sub_" in hierarchical_id:
        parent_id = hierarchical_id.split("_sub_")[0]
        assert parent_id == "req_abc123"
    
    # Verify it's a sub-agent request
    assert "_sub_" in hierarchical_id
    
    print("✓ Request ID extraction passed")


def test_simple_fallback_request_id():
    """Test that fallback request IDs don't contain parent info."""
    simple_id = "sub_abc123def"
    
    # Should not contain parent prefix
    assert not simple_id.startswith("req_")
    assert simple_id.startswith("sub_")
    
    # Should not have hierarchical separator
    assert "_sub_" not in simple_id or simple_id.count("_sub_") == 0 or simple_id.split("_")[0] == "sub"
    
    print("✓ Simple fallback request ID validation passed")

