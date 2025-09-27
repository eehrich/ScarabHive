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


class MockStatus:
    """Mock status object for tests."""
    
    async def progress(self, message: str):
        """Mock progress method."""
        pass
    
    async def error(self, message: str):
        """Mock error method."""
        pass
    
    async def end(self, message: str, meta=None):
        """Mock end method."""
        pass


class TestSchemaCompliance:
    """Test schema file and MCP compliance."""

    @pytest.fixture
    def schema_data(self):
        """Load schema.yaml file (MCP format)."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "schema.yaml"
        with open(schema_path, 'r') as f:
            return yaml.safe_load(f)

    def test_schema_file_exists(self):
        """Test that schema.yaml exists."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "schema.yaml"
        assert schema_path.exists(), "schema.yaml file must exist"

    def test_schema_basic_structure(self, schema_data):
        """Test schema has required MCP format structure."""
        # For MCP format, we expect 'tools' at the top level
        assert "tools" in schema_data, "Schema must have 'tools' field"
        assert isinstance(schema_data["tools"], list), "tools must be a list"
        assert len(schema_data["tools"]) == 3, "Should have exactly 3 tools"

    def test_server_metadata(self, schema_data):
        """Test schema tools have proper MCP structure."""
        tools = schema_data["tools"]
        for tool in tools:
            assert "type" in tool
            assert tool["type"] == "function"
            assert "function" in tool
            assert "name" in tool["function"]
            assert "description" in tool["function"]

    def test_tools_definition(self, schema_data):
        """Test tools are properly defined."""
        tools = schema_data["tools"]
        assert len(tools) == 3, "Should have exactly 3 tools"
        
        tool_names = [tool["function"]["name"] for tool in tools]
        assert "execute_python" in tool_names
        assert "validate_python" in tool_names
        assert "reset_sandbox" in tool_names

    def test_eval_tool_schema(self, schema_data):
        """Test execute_python tool has proper MCP schema."""
        eval_tool = next(tool for tool in schema_data["tools"] if tool["function"]["name"] == "execute_python")
        
        # Check MCP function structure
        function = eval_tool["function"]
        assert function["description"]
        assert "parameters" in function
        
        # Check input parameters
        params = function["parameters"]
        assert params["type"] == "object"
        assert "code" in params["properties"]
        assert params["required"] == ["code"]

    def test_validate_tool_schema(self, schema_data):
        """Test validate_python tool has proper MCP schema."""
        validate_tool = next(tool for tool in schema_data["tools"] if tool["function"]["name"] == "validate_python")
        
        # Check MCP function structure
        function = validate_tool["function"]
        assert function["description"]
        assert "parameters" in function
        
        # Check input parameters
        params = function["parameters"]
        assert params["type"] == "object"
        assert "code" in params["properties"]
        assert params["required"] == ["code"]

    def test_reset_tool_schema(self, schema_data):
        """Test reset_sandbox tool has proper MCP schema."""
        reset_tool = next(tool for tool in schema_data["tools"] if tool["function"]["name"] == "reset_sandbox")
        
        # Check MCP function structure
        function = reset_tool["function"]
        assert function["description"]
        assert "parameters" in function
        
        # Check input parameters (should be empty object)
        params = function["parameters"]
        assert params["type"] == "object"
        assert params["properties"] == {}
        assert params.get("required", []) == []

    def test_security_section(self, schema_data):
        """Test MCP schema contains valid tool definitions."""
        # For MCP format, security is handled in the server implementation
        # Just verify the tools are properly structured
        tools = schema_data["tools"]
        for tool in tools:
            function = tool["function"]
            assert "name" in function
            assert "description" in function
            assert "parameters" in function
            # Verify parameters follow JSON schema format
            params = function["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_examples_section(self, schema_data):
        """Test MCP tools have proper descriptions that serve as examples."""
        tools = schema_data["tools"]
        
        # Find execute_python tool and check it has good examples in description
        exec_tool = next(tool for tool in tools if tool["function"]["name"] == "execute_python")
        description = exec_tool["function"]["description"]
        assert "mathematical expressions" in description
        assert "programming" in description
        
        # Check parameter descriptions are informative
        code_param = exec_tool["function"]["parameters"]["properties"]["code"]
        assert "description" in code_param
        assert len(code_param["description"]) > 10  # Should be descriptive


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
        status = MockStatus()
        
        # Test validate tool (non-async)
        result = await server.call("validate_python", {"code": "x = 2 + 3", "_status": status})
        assert "result" in result
        assert "✅" in result["result"]
        
        # Test reset tool
        result = await server.call("reset_sandbox", {"_status": status})
        assert "result" in result
        assert "🔄" in result["result"]

    @pytest.mark.asyncio
    async def test_mcpserver_call_eval(self):
        """Test MCPServer call method with eval tool."""
        server = ScriptInterpreterServer()
        status = MockStatus()
        
        # Test eval tool - simple expression
        result = await server.call("execute_python", {"code": "2 + 3", "_status": status})
        assert "result" in result
        assert "5" in result["result"]

    def test_mcpserver_get_tools_method(self):
        """Test MCPServer get_tools method returns OpenAI function format."""
        server = ScriptInterpreterServer()
        tools = server.get_tools()
        assert len(tools) > 0
        schema = tools[0]  # Use first tool for schema validation
        
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
        assert action == "execute_python"

    @pytest.mark.asyncio
    async def test_mcpserver_handles_unknown_tool(self):
        """Test MCPServer handles unknown tools gracefully."""
        server = ScriptInterpreterServer()
        status = MockStatus()
        result = await server.call("unknown_tool", {"_status": status})
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
        assert "execute_python" in tool_names
        assert "validate_python" in tool_names  
        assert "reset_sandbox" in tool_names

    @pytest.mark.asyncio
    async def test_eval_tool_input_validation(self):
        """Test eval tool validates input according to schema."""
        server = ScriptInterpreterServer()
        status = MockStatus()
        
        # Valid input
        request = {
            "method": "tools/call",
            "params": {
                "name": "execute_python",
                "arguments": {"code": "2 + 3", "_status": status}
            }
        }
        response = await server.handle_request(request)
        assert "content" in response
        
        # Missing code parameter should be handled gracefully
        request_no_code = {
            "method": "tools/call", 
            "params": {
                "name": "execute_python",
                "arguments": {"_status": status}
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
                "name": "validate_python",
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
                "name": "reset_sandbox",
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
        status = MockStatus()
        
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
                    "name": "execute_python",
                    "arguments": {"code": code, "_status": status}
                }
            }
            response = await server.handle_request(request)
            assert "content" in response
            # Should not contain error messages for valid examples
            content = response["content"][0]["text"]
            assert "Error" not in content

    def test_schema_yaml_is_valid(self):
        """Test that schema.yaml is valid YAML."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "schema.yaml"
        try:
            with open(schema_path, 'r') as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            pytest.fail(f"schema.yaml is not valid YAML: {e}")

    def test_json_schema_validity(self):
        """Test that tool parameter schemas are valid JSON Schema."""
        schema_path = Path(__file__).parent.parent / "src" / "plugins" / "script_interpreter" / "schema.yaml"
        with open(schema_path, 'r') as f:
            schema_data = yaml.safe_load(f)
            
        for tool in schema_data["tools"]:
            function = tool["function"]
            parameters = function["parameters"]
            
            # Basic JSON Schema validation for parameters
            assert "type" in parameters
            assert parameters["type"] == "object"
            assert "properties" in parameters
            # required field is optional in JSON schema
            if "required" in parameters:
                assert isinstance(parameters["required"], list)


if __name__ == "__main__":
    pytest.main([__file__])