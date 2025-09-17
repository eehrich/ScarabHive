# Example MCP Plugin

This is a comprehensive example plugin that demonstrates best practices for developing Model Context Protocol (MCP) plugins for the AgentSystem. It serves as a reference implementation and template for creating new plugins.

## Overview

The example plugin provides three demonstration tools:
- **Calculator**: Performs basic arithmetic operations (add, subtract, multiply, divide)
- **Text Formatter**: Formats text in various ways (uppercase, lowercase, title case, reverse)
- **Status Reporter**: Provides plugin status and configuration information

## Plugin Architecture

This plugin demonstrates the complete MCP plugin architecture:

```
src/plugins/example/
├── __init__.py           # Package initialization
├── plugin.py             # Plugin factory and entry point
├── plugin.yaml           # Plugin metadata and configuration
├── schema.yaml           # Tool schema definitions (optional)
├── server.py             # Core plugin server implementation
├── README.md             # This documentation
└── tests/                # Plugin-specific tests
    ├── __init__.py
    ├── test_calculator.py
    ├── test_formatter.py
    └── test_integration.py
```

## Key Features Demonstrated

### 1. Multi-Tool Plugin
- Implements multiple tools in a single plugin
- Shows how to route tool calls to specific handlers
- Demonstrates tool schema definition and validation

### 2. Schema Management
- External schema definition in `schema.yaml`
- Template variable replacement (`{name}` → plugin instance name)
- Fallback to inline schema definitions

### 3. Error Handling
- Input validation with clear error messages
- Graceful handling of edge cases (division by zero, invalid operations)
- Proper exception propagation

### 4. Configuration Support
- Plugin configuration via `config/agent.yaml`
- Environment variable overrides
- Default value handling

### 5. Logging and Debugging
- Structured logging with context
- Debug information for development
- Performance monitoring capabilities

## Usage Examples

### Calculator Tool
```python
# Add two numbers
result = await plugin.call("example_calculator", {
    "operation": "add",
    "a": 10,
    "b": 5
})
# Returns: {"operation": "add", "operands": [10.0, 5.0], "result": 15.0}

# Divide with error handling
try:
    result = await plugin.call("example_calculator", {
        "operation": "divide",
        "a": 10,
        "b": 0
    })
except ValueError as e:
    print(f"Error: {e}")  # "Division by zero"
```

### Text Formatter Tool
```python
# Format text to uppercase
result = await plugin.call("example_formatter", {
    "text": "hello world",
    "format": "uppercase"
})
# Returns: {"original": "hello world", "format": "uppercase", "formatted": "HELLO WORLD"}

# Reverse text
result = await plugin.call("example_formatter", {
    "text": "hello",
    "format": "reverse"
})
# Returns: {"original": "hello", "format": "reverse", "formatted": "olleh"}
```

### Status Tool
```python
# Get basic status
result = await plugin.call("example_status", {"verbose": False})
# Returns: {"server_name": "example", "status": "active", "tools_count": 3}

# Get detailed status
result = await plugin.call("example_status", {"verbose": True})
# Returns detailed information including config and available tools
```

## Development Guidelines

### 1. Plugin Structure
- Inherit from `MCPServer` base class
- Implement `get_tools()` for multi-tool support
- Provide `get_schema()` and `get_default_action()` for compatibility
- Use async methods for all operations

### 2. Schema Definition
- Define tools in external `schema.yaml` when possible
- Use template variables for dynamic naming
- Provide comprehensive parameter descriptions
- Include validation constraints (enums, required fields)

### 3. Error Handling
- Validate all input parameters
- Provide clear, user-friendly error messages
- Use appropriate exception types (ValueError, TypeError, etc.)
- Log errors with sufficient context

### 4. Testing
- Write comprehensive unit tests for each tool
- Include integration tests with real plugin instances
- Test error conditions and edge cases
- Use pytest async features for async code

### 5. Documentation
- Document all public methods and classes
- Provide usage examples in docstrings
- Include configuration options and defaults
- Explain error conditions and return values

## Configuration

The plugin supports configuration via `config/agent.yaml`:

```yaml
plugins:
  example:
    # Plugin-specific configuration
    precision: 2              # Decimal precision for calculations
    max_text_length: 1000     # Maximum text length for formatting
    enable_debug: false       # Enable debug logging
```

Environment variables:
- `EXAMPLE_PRECISION`: Override calculation precision
- `EXAMPLE_MAX_TEXT_LENGTH`: Override max text length
- `EXAMPLE_DEBUG`: Enable debug mode

## Testing

Run the plugin tests:

```bash
# Run all example plugin tests
pytest src/plugins/example/tests/ -v

# Run specific test file
pytest src/plugins/example/tests/test_calculator.py -v

# Run with coverage
pytest src/plugins/example/tests/ --cov=src.plugins.example --cov-report=html
```

## Command Line Interface

The plugin provides CLI commands for testing and debugging:

```bash
# Test calculator functionality
python -m plugins.example.cli calculator --operation add --a 10 --b 5

# Test text formatter
python -m plugins.example.cli formatter --text "hello world" --format uppercase

# Get plugin status
python -m plugins.example.cli status --verbose
```

## Best Practices Demonstrated

1. **Separation of Concerns**: Clear separation between plugin factory, server logic, and tool handlers
2. **Configuration Management**: Flexible configuration with multiple sources and validation
3. **Error Handling**: Comprehensive error handling with user-friendly messages
4. **Testing**: Complete test coverage with unit and integration tests
5. **Documentation**: Thorough documentation with examples and usage patterns
6. **Schema Management**: External schema definitions with template support
7. **Logging**: Structured logging for debugging and monitoring
8. **Type Safety**: Full type hints and validation
9. **Async Support**: Proper async/await patterns throughout
10. **Extensibility**: Easy to extend with additional tools and functionality

## Extension Points

To add new tools to this plugin:

1. Add tool definition to `schema.yaml`
2. Implement handler method in `server.py`
3. Add routing logic in `call()` method
4. Write tests for the new tool
5. Update documentation

Example of adding a new tool:

```python
# In schema.yaml
- type: function
  function:
    name: "{name}_hasher"
    description: "Generate hash of input text"
    parameters:
      type: object
      properties:
        text:
          type: string
          description: "Text to hash"
        algorithm:
          type: string
          enum: ["md5", "sha1", "sha256"]
          description: "Hash algorithm"
      required: ["text", "algorithm"]

# In server.py
async def _handle_hasher(self, params: dict[str, Any]) -> dict[str, Any]:
    """Handle hash generation."""
    import hashlib
    text = params["text"]
    algorithm = params["algorithm"]
    
    hash_func = getattr(hashlib, algorithm)
    result = hash_func(text.encode()).hexdigest()
    
    return {
        "text": text,
        "algorithm": algorithm,
        "hash": result
    }
```

This example plugin demonstrates all the essential patterns and practices for building robust, maintainable MCP plugins.