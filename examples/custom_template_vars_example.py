#!/usr/bin/env python3
"""
Example showing how to use custom template variables in schema.yaml files.

This demonstrates the new get_template_vars() method which eliminates the need
to override the entire _load_schema() method just to add custom template variables.
"""

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from typing import Any


class ExampleCustomTemplateServer(SchemaBasedMCPServer):
    """Example server that demonstrates custom template variables."""
    
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True):
        super().__init__(name, config, ssl_verify)
        
        # Example configuration values that we want to use in schema templates
        self.max_retries = int(self.config.get("max_retries", 3))
        self.timeout_seconds = float(self.config.get("timeout_seconds", 30.0))
        self.allowed_domains = self.config.get("allowed_domains", ["example.com", "test.org"])
    
    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for schema rendering.
        
        This is the NEW, RECOMMENDED way to add custom template variables.
        Much cleaner than overriding _load_schema()!
        """
        return {
            "name": self.name,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "allowed_domains": self.allowed_domains
        }
    
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Handle tool calls."""
        if tool == f"{self.name}_fetch":
            return {"status": "success", "message": f"Fetching with max_retries={self.max_retries}"}
        elif tool == f"{self.name}_validate":
            return {"status": "success", "message": f"Validating with timeout={self.timeout_seconds}s"}
        else:
            return {"status": "error", "error": f"Unknown tool: {tool}"}


# Example schema.yaml content that would use these template variables:
EXAMPLE_SCHEMA_YAML = """
tools:
  - type: function
    function:
      name: "{{ name }}_fetch"
      description: Fetch data from allowed domains with retry logic
      parameters:
        type: object
        properties:
          url:
            type: string
            format: uri
            description: "URL to fetch (must be from allowed domains)"
          retries:
            type: integer
            minimum: 1
            maximum: {{ max_retries }}
            default: {{ max_retries }}
            description: "Number of retries (1 to {{ max_retries }})"
          timeout:
            type: number
            minimum: 1
            maximum: {{ timeout_seconds }}
            default: {{ timeout_seconds }}
            description: "Request timeout in seconds"
        required: ["url"]
        additionalProperties: false

  - type: function
    function:
      name: "{{ name }}_validate"
      description: Validate URL against allowed domains
      parameters:
        type: object
        properties:
          url:
            type: string
            format: uri
            description: "URL to validate"
        required: ["url"]
        additionalProperties: false
"""


def main():
    """Demonstrate the template variable functionality."""
    print("=== Custom Template Variables Example ===\n")
    
    # Create server with custom configuration
    config = {
        "max_retries": 5,
        "timeout_seconds": 60.0,
        "allowed_domains": ["api.example.com", "secure.test.org"]
    }
    
    server = ExampleCustomTemplateServer("my_fetcher", config)
    
    # Show the template variables that would be provided
    template_vars = server.get_template_vars()
    print("Template variables provided to schema.yaml:")
    for key, value in template_vars.items():
        print(f"  {key}: {value}")
    
    print("\nExample schema.yaml content:")
    print(EXAMPLE_SCHEMA_YAML)
    
    print("After template rendering, the schema would have:")
    print("  - Tool name: my_fetcher_fetch")
    print("  - Max retries: 5")
    print("  - Max timeout: 60.0 seconds")
    print("  - Default retries: 5")
    print("  - Default timeout: 60.0 seconds")


if __name__ == "__main__":
    main()