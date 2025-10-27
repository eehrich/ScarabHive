#!/usr/bin/env python3
"""Test script to check tools prompt."""
import asyncio
import sys
sys.path.insert(0, 'src')

from agent_system.servers.agent.tool_schema_builder import ToolSchemaBuilder
from agent_system.servers.agent.components.mcp_integration import MCPIntegrationManager


async def main():
    # Simulate the flow
    available_tools_input = [
        "duckduckgo_search",
        "basic_operations", 
        "web_scraper"
    ]
    
    print("Input to build_schemas:")
    print(f"  {', '.join(available_tools_input)}")
    print()
    
    # Note: We can't actually run build_schemas without full setup
    # But we can show what SHOULD happen based on our changes
    
    print("Expected output (individual tool names only):")
    print("  - Server 'duckduckgo_search' → tool 'duckduckgo_search_web_search'")
    print("  - Server 'basic_operations' → tools 'wait', 'ping'")
    print("  - Server 'web_scraper' → tool 'scrape_webpage'")
    print()
    print("BEFORE fix: prompt would show:")
    print("  Available Tools: duckduckgo_search, basic_operations, web_scraper, duckduckgo_search_web_search, wait, ping, scrape_webpage")
    print()
    print("AFTER fix: prompt should show:")
    print("  Available Tools: duckduckgo_search_web_search, wait, ping, scrape_webpage")
    

if __name__ == "__main__":
    asyncio.run(main())
