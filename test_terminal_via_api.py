"""
Test terminal plugin loading
"""
import asyncio
import sys
import os
sys.path.insert(0, 'src')

# Set environment
os.environ['CONFIG_PATH'] = 'config/config.yaml'


async def main():
    print("=== Testing Terminal Plugin Loading ===\n")
    
    from agent_system.core.plugin_loader import PluginLoader
    
    loader = PluginLoader()
    plugins = await loader.load_all_plugins()
    
    print(f"Total plugins loaded: {len(plugins)}")
    
    # Find terminal plugin
    terminal_found = False
    for plugin_name in sorted(plugins.keys()):
        if 'terminal' in plugin_name.lower():
            terminal_found = True
            plugin = plugins[plugin_name]
            print(f"\n✓ Found Terminal Plugin: {plugin_name}")
            print(f"  Module: {plugin.get('module', 'unknown')}")
            print(f"  Type: {plugin.get('type', 'unknown')}")
            
            # Get server instance
            server = plugin.get('server')
            if server:
                print(f"  Server: {server.__class__.__name__}")
                
                # Try to list tools
                try:
                    if hasattr(server, 'list_tools'):
                        tools = await server.list_tools()
                        print(f"  Tools: {len(tools)}")
                        for tool in tools[:5]:  # Show first 5
                            tool_info = tool.get('function', {})
                            print(f"    - {tool_info.get('name', 'unknown')}: {tool_info.get('description', '')[:60]}")
                except Exception as e:
                    print(f"  Error listing tools: {e}")
    
    if not terminal_found:
        print("\n✗ Terminal plugin not found!")
        print("\nAvailable plugins:")
        for name in sorted(plugins.keys())[:20]:
            print(f"  - {name}")


if __name__ == "__main__":
    asyncio.run(main())
