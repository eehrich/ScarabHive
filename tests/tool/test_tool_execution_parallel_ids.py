"""
Test parallel tool call visibility with unique request_id suffixes
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_system.servers.agent.components.tool_execution import ToolExecutionManager


@pytest.fixture
def mock_registry():
    """Mock plugin registry"""
    registry = MagicMock()
    registry.list.return_value = ["web_scraper", "test_tool"]
    
    # Mock web_scraper server
    mock_server = MagicMock()  # Use MagicMock first, then convert specific methods to AsyncMock
    mock_server.get_default_action.return_value = "fetch"
    # Make call_with_status an AsyncMock
    mock_server.call_with_status = AsyncMock(return_value={"content": "scraped content"})
    
    # Make sure registry.get() returns the same mock server for all calls
    registry.get.return_value = mock_server
    
    return registry


@pytest.fixture
def tool_execution_manager(mock_registry):
    """Create ToolExecutionManager with mocked registry"""
    return ToolExecutionManager(mock_registry)


@pytest.mark.asyncio
async def test_parallel_tool_calls_get_unique_request_id_suffixes(tool_execution_manager):
    """Test that parallel tool calls get unique request_id suffixes for status visibility"""
    
    # Create multiple parallel tool calls (simulating 27 web_scraper calls)
    tool_calls = []
    for i in range(5):  # Use 5 instead of 27 for faster testing
        tool_calls.append({
            "id": f"call_{i}",
            "function": {
                "name": "web_scraper",
                "arguments": json.dumps({
                    "url": f"https://example{i}.com",
                    "request_id": "test_request_123",  # Same base request_id for all
                    "action": "fetch"
                })
            }
        })
    
    tool_name_mapping = {"web_scraper": "web_scraper"}
    available_tools = ["web_scraper"]
    step = 1
    
    # Track the actual params passed to call_with_status
    called_params = []
    
    def capture_call_with_status_params(action, params):
        called_params.append(params.copy())
        return {"content": f"scraped content {len(called_params)}"}
    
    # Ensure the mock server is properly configured
    mock_server = tool_execution_manager.registry.get("web_scraper")
    mock_server.call_with_status.side_effect = capture_call_with_status_params
    
    # Execute the tools
    tool_messages, events, results = await tool_execution_manager.execute_tools(
        tool_calls, tool_name_mapping, available_tools, step
    )
    
    print(f"Called params count: {len(called_params)}")
    print(f"Tool messages count: {len(tool_messages)}")
    
    # Verify all 5 calls were made
    assert len(called_params) == 5
    assert len(tool_messages) == 5
    
    # Verify each call got a unique request_id suffix
    request_ids = [params.get("request_id") for params in called_params]
    
    print(f"Request IDs: {request_ids}")
    
    # Check that all request_ids are unique
    assert len(set(request_ids)) == 5, f"Expected 5 unique request_ids, got {len(set(request_ids))}"
    
    # Check the format of request_id suffixes
    expected_request_ids = [f"test_request_123_{i+1:03d}" for i in range(5)]
    assert sorted(request_ids) == sorted(expected_request_ids)
    
    # Verify that both snake_case and camelCase versions are set
    for params in called_params:
        assert params.get("request_id") == params.get("requestId")
        assert params.get("request_id").startswith("test_request_123_")
    
    print("✅ Test passed: All 5 parallel calls got unique request_id suffixes")
    print(f"   Request_ids: {request_ids}")


@pytest.mark.asyncio
async def test_single_tool_call_gets_consistent_suffix(tool_execution_manager):
    """Test that single tool calls get consistent suffix behavior like multiple tool calls"""
    
    # Create single tool call
    tool_calls = [{
        "id": "call_single",
        "function": {
            "name": "web_scraper", 
            "arguments": json.dumps({
                "url": "https://example.com",
                "request_id": "single_request_456",
                "action": "fetch"
            })
        }
    }]
    
    tool_name_mapping = {"web_scraper": "web_scraper"}
    available_tools = ["web_scraper"]
    step = 1
    
    # Track the actual params passed to call_with_status
    called_params = []
    
    def capture_call_with_status_params(action, params):
        called_params.append(params.copy())
        return {"content": "scraped content"}
    
    mock_server = tool_execution_manager.registry.get("web_scraper")
    mock_server.call_with_status.side_effect = capture_call_with_status_params
    
    # Execute the tool
    tool_messages, events, results = await tool_execution_manager.execute_tools(
        tool_calls, tool_name_mapping, available_tools, step
    )
    
    # Verify single call was made  
    assert len(called_params) == 1
    
    # Verify request_id gets suffix even for single calls (consistent behavior)
    request_id = called_params[0].get("request_id")
    assert request_id == "single_request_456_001", f"Expected request_id with suffix, got {request_id}"
    
    print(f"✅ Test passed: Single tool call got consistent suffix: {request_id}")


@pytest.mark.asyncio 
async def test_no_request_id_in_params_handles_gracefully(tool_execution_manager):
    """Test that tool calls without request_id in params handle gracefully"""
    
    # Create tool calls without request_id
    tool_calls = []
    for i in range(3):
        tool_calls.append({
            "id": f"call_{i}",
            "function": {
                "name": "web_scraper",
                "arguments": json.dumps({
                    "url": f"https://example{i}.com",
                    "action": "fetch"
                    # No request_id
                })
            }
        })
    
    tool_name_mapping = {"web_scraper": "web_scraper"}
    available_tools = ["web_scraper"]
    step = 1
    
    # Track the actual params passed to call_with_status
    called_params = []
    
    def capture_call_with_status_params(action, params):
        called_params.append(params.copy())
        return {"content": f"scraped content {len(called_params)}"}
    
    mock_server = tool_execution_manager.registry.get("web_scraper")
    mock_server.call_with_status.side_effect = capture_call_with_status_params
    
    # Execute the tools
    tool_messages, events, results = await tool_execution_manager.execute_tools(
        tool_calls, tool_name_mapping, available_tools, step
    )
    
    # Verify all calls were made
    assert len(called_params) == 3
    
    # Verify that params don't have request_id (None values)
    for params in called_params:
        assert params.get("request_id") is None
        assert params.get("requestId") is None
    
    print("✅ Test passed: Tool calls without request_id handled gracefully")


if __name__ == "__main__":
    # Run the tests directly
    import sys
    sys.exit(pytest.main([__file__, "-v"]))