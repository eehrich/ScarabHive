"""
Test suite for script interpreter MCP schema compliance and tool definitions.

Tests:
- Schema file validation
- Tool input/output schema compliance  
- MCP protocol conformance
- Tool examples validation
"""

import sys
from pathlib import Path

import pytest
import yaml

# Add src to path for plugin imports
src_path = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src_path))

# Import after path modification
from plugins.script_interpreter.server import ScriptInterpreterServer  # noqa: E402


class TestSchemaCompliance:
    """Test schema file and MCP compliance."""

    @pytest.fixture
    def schema_data(self):
        """Load mcp_schema.yaml file (MCP format)."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "mcp_schema.yaml"
        with open(schema_path, 'r') as f:
            return yaml.safe_load(f)

    def test_schema_file_exists(self):
        """Test that mcp_schema.yaml exists."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "mcp_schema.yaml"
        assert schema_path.exists(), "mcp_schema.yaml file must exist"

    def test_schema_basic_structure(self, schema_data):
        """Test schema has required top-level fields."""
        required_fields = ["name", "version", "description", "server", "tools"]
        for field in required_fields:
            assert field in schema_data, f"Schema must have '{field}' field"

    def test_server_metadata(self, schema_data):
        """Test server metadata is properly defined."""
        server = schema_data["server"]
        assert "name" in server
        assert "version" in server
        assert "description" in server
        assert server["name"] == "Script Interpreter"

    def test_tools_definition(self, schema_data):
        """Test tools are properly defined."""
        tools = schema_data["tools"]
        assert len(tools) == 3, "Should have exactly 3 tools"
        
        tool_names = [tool["name"] for tool in tools]
        assert "eval" in tool_names
        assert "validate" in tool_names
        assert "reset" in tool_names

    def test_eval_tool_schema(self, schema_data):
        """Test eval tool has proper schema."""
        eval_tool = next(tool for tool in schema_data["tools"] if tool["name"] == "eval")
        
        # Check input schema
        input_schema = eval_tool["inputSchema"]
        assert input_schema["type"] == "object"
        assert "code" in input_schema["properties"]
        assert input_schema["required"] == ["code"]
        
        # Check output schema
        output_schema = eval_tool["outputSchema"]
        assert output_schema["type"] == "object"
        assert "result" in output_schema["properties"]
        assert "error" in output_schema["properties"]

    def test_validate_tool_schema(self, schema_data):
        """Test validate tool has proper schema."""
        validate_tool = next(tool for tool in schema_data["tools"] if tool["name"] == "validate")
        
        # Check input schema
        input_schema = validate_tool["inputSchema"]
        assert input_schema["type"] == "object"
        assert "code" in input_schema["properties"]
        assert input_schema["required"] == ["code"]
        
        # Check output schema
        output_schema = validate_tool["outputSchema"]
        assert output_schema["type"] == "object"
        assert "valid" in output_schema["properties"]
        assert "message" in output_schema["properties"]

    def test_reset_tool_schema(self, schema_data):
        """Test reset tool has proper schema."""
        reset_tool = next(tool for tool in schema_data["tools"] if tool["name"] == "reset")
        
        # Check input schema (should be empty)
        input_schema = reset_tool["inputSchema"]
        assert input_schema["type"] == "object"
        assert input_schema["properties"] == {}
        assert input_schema["required"] == []
        
        # Check output schema
        output_schema = reset_tool["outputSchema"]
        assert output_schema["type"] == "object"
        assert "message" in output_schema["properties"]
        assert "status" in output_schema["properties"]

    def test_security_section(self, schema_data):
        """Test security configuration is documented."""
        security = schema_data["security"]
        assert security["sandbox"] is True
        assert security["timeout"] == 5.0
        assert security["network_access"] is False
        assert "allowed_functions" in security
        assert len(security["allowed_functions"]) > 0

    def test_examples_section(self, schema_data):
        """Test examples are provided for all tools."""
        examples = schema_data["examples"]
        assert "eval" in examples
        assert "validate" in examples
        assert "reset" in examples
        
        # Check eval examples
        eval_examples = examples["eval"]
        assert len(eval_examples) >= 3
        for example in eval_examples:
            assert "code" in example
            assert "description" in example


class TestMCPServerIntegration:
    """Test MCPServer integration and compatibility."""

    def test_server_inherits_from_mcpserver(self):
        """Test that ScriptInterpreterServer inherits from MCPServer."""
        from agent_system.mcp.base import MCPServer
        server = ScriptInterpreterServer()
        assert isinstance(server, MCPServer)

    @pytest.mark.asyncio
    async def test_mcpserver_call_method(self):
        """Test MCPServer call method."""
        server = ScriptInterpreterServer()
        
        # Test validate tool (non-async)
        result = await server.call("validate", {"code": "x = 2 + 3"})
        assert "result" in result
        assert "✅" in result["result"]
        
        # Test reset tool
        result = await server.call("reset", {})
        assert "result" in result
        assert "🔄" in result["result"]

    @pytest.mark.asyncio
    async def test_mcpserver_call_eval(self):
        """Test MCPServer call method with eval tool."""
        server = ScriptInterpreterServer()
        
        # Test eval tool - simple expression
        result = await server.call("eval", {"code": "2 + 3"})
        assert "result" in result
        assert "5" in result["result"]

    def test_mcpserver_get_schema_method(self):
        """Test MCPServer get_schema method returns OpenAI function format."""
        server = ScriptInterpreterServer()
        schema = server.get_schema()
        
        assert "type" in schema
        assert schema["type"] == "function"
        assert "function" in schema
        assert "name" in schema["function"]
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]

    def test_mcpserver_get_default_action(self):
        """Test MCPServer get_default_action method."""
        server = ScriptInterpreterServer()
        action = server.get_default_action()
        assert action == "eval"

    @pytest.mark.asyncio
    async def test_mcpserver_handles_unknown_tool(self):
        """Test MCPServer handles unknown tools gracefully."""
        server = ScriptInterpreterServer()
        result = await server.call("unknown_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]


class TestMCPProtocolCompliance:
    """Test MCP protocol compliance with live server."""

    @pytest.mark.asyncio
    async def test_server_tools_match_schema(self):
        """Test that server tools match schema definitions."""
        server = ScriptInterpreterServer()
        tools_response = await server._list_tools()
        tools = tools_response["tools"]
        
        # Should have 3 tools
        assert len(tools) == 3
        
        tool_names = [tool["name"] for tool in tools]
        assert "eval" in tool_names
        assert "validate" in tool_names  
        assert "reset" in tool_names

    @pytest.mark.asyncio
    async def test_eval_tool_input_validation(self):
        """Test eval tool validates input according to schema."""
        server = ScriptInterpreterServer()
        
        # Valid input
        request = {
            "method": "tools/call",
            "params": {
                "name": "eval",
                "arguments": {"code": "2 + 3"}
            }
        }
        response = await server.handle_request(request)
        assert "content" in response
        
        # Missing code parameter should be handled gracefully
        request_no_code = {
            "method": "tools/call", 
            "params": {
                "name": "eval",
                "arguments": {}
            }
        }
        response = await server.handle_request(request_no_code)
        # Should have error handling for missing required parameter

    @pytest.mark.asyncio
    async def test_validate_tool_input_validation(self):
        """Test validate tool validates input according to schema."""
        server = ScriptInterpreterServer()
        
        # Valid input
        request = {
            "method": "tools/call",
            "params": {
                "name": "validate",
                "arguments": {"code": "x = 2 + 3"}
            }
        }
        response = await server.handle_request(request)
        assert "content" in response
        content = response["content"][0]["text"]
        assert "✅" in content or "valid" in content.lower()

    @pytest.mark.asyncio
    async def test_reset_tool_empty_input(self):
        """Test reset tool accepts empty input according to schema."""
        server = ScriptInterpreterServer()
        
        # Empty arguments (as per schema)
        request = {
            "method": "tools/call",
            "params": {
                "name": "reset",
                "arguments": {}
            }
        }
        response = await server.handle_request(request)
        assert "content" in response
        content = response["content"][0]["text"]
        assert "🔄" in content or "reset" in content.lower()

    @pytest.mark.asyncio
    async def test_schema_examples_work(self):
        """Test that schema examples actually work with the server."""
        server = ScriptInterpreterServer()
        
        # Test eval examples
        examples = [
            "(2 + 3) * 4",
            "x = 42\ny = x * 2\nabs(y - 100)",
            "sum([1, 2, 3, 4, 5])"
        ]
        
        for code in examples:
            request = {
                "method": "tools/call",
                "params": {
                    "name": "eval",
                    "arguments": {"code": code}
                }
            }
            response = await server.handle_request(request)
            assert "content" in response
            # Should not contain error messages for valid examples
            content = response["content"][0]["text"]
            assert "Error" not in content

    def test_schema_yaml_is_valid(self):
        """Test that mcp_schema.yaml is valid YAML."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "mcp_schema.yaml"
        try:
            with open(schema_path, 'r') as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            pytest.fail(f"mcp_schema.yaml is not valid YAML: {e}")

    def test_json_schema_validity(self):
        """Test that tool schemas are valid JSON Schema."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "mcp_schema.yaml"
        with open(schema_path, 'r') as f:
            schema_data = yaml.safe_load(f)
            
        for tool in schema_data["tools"]:
            input_schema = tool["inputSchema"]
            output_schema = tool["outputSchema"]
            
            # Basic JSON Schema validation
            assert "type" in input_schema
            assert input_schema["type"] == "object"
            assert "properties" in input_schema
            assert "required" in input_schema
            
            assert "type" in output_schema
            assert output_schema["type"] == "object"
            assert "properties" in output_schema


if __name__ == "__main__":
    pytest.main([__file__])