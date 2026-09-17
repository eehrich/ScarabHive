"""
E2E Validation Script for Memory Plugin

Tests plugin integration with AgentSystem:
- Plugin discovery
- Tool registration
- Basic operations (store, recall, search)
- Config loading
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from plugins.memory.server import MemoryServer
from plugins.memory.plugin import PLUGIN_FACTORY, MemoryManagementHybridPlugin


async def validate_e2e():
    """End-to-end validation of Memory Plugin"""
    
    print("=" * 60)
    print("Memory Plugin - E2E Validation")
    print("=" * 60)
    
    # 1. Plugin Discovery
    print("\n[1] Plugin Discovery")
    print("- Factory function exists: PLUGIN_FACTORY")
    assert PLUGIN_FACTORY is not None
    print("✓ Factory function found")
    
    # 2. Create Mock Configs
    print("\n[2] Configuration")
    mock_sys_config = type('obj', (object,), {})()
    mock_server_config = type('obj', (object,), {
        'storage_path': './data/memories',
        'max_memories': 10,
        'max_memories_per_session': 1000,
        'auto_extract_keywords': True,
        'search_n_results': 5,
        'use_semantic_injection': True,
    })()
    print("✓ Mock configurations created")
    
    # 3. Instantiate Plugin
    print("\n[3] Plugin Instantiation")
    plugin = PLUGIN_FACTORY(
        name="memory",
        system_config=mock_sys_config,
        server_config=mock_server_config,
    )
    assert isinstance(plugin, MemoryManagementHybridPlugin)
    print(f"✓ Plugin instance: {type(plugin).__name__}")
    
    # 4. Check Components
    print("\n[4] Plugin Components")
    assert hasattr(plugin, 'server')
    assert isinstance(plugin.server, MemoryServer)
    print(f"✓ Tool server: {type(plugin.server).__name__}")
    
    assert hasattr(plugin, 'web_factory')
    print(f"✓ Web Factory: {type(plugin.web_factory).__name__}")
    
    # 5. Tool Registration
    print("\n[5] Tool Registration")
    tools = plugin.get_tools()
    assert len(tools) > 0
    tool_names = [t['name'] for t in tools]
    print(f"✓ Registered tools: {tool_names}")
    
    # Check unified memory tool exists
    memory_tool = next((t for t in tools if t['name'] == 'memory'), None)
    assert memory_tool is not None
    print(f"✓ Memory tool found with {len(memory_tool['inputSchema']['properties'])} parameters")
    
    # 6. Storage Operations
    print("\n[6] Storage Operations")
    session_id = "e2e_test_session"
    
    # Store
    store_result = await plugin.call_tool("memory", {
        "operation": "store",
        "session_id": session_id,
        "title": "E2E Test Memory",
        "content": "This memory validates end-to-end integration",
        "importance": 8,
        "tags": ["e2e", "validation"]
    })
    assert "memory_id" in store_result
    memory_id = store_result["memory_id"]
    print(f"✓ Stored memory: {memory_id}")
    
    # Recall
    recall_result = await plugin.call_tool("memory", {
        "operation": "recall",
        "session_id": session_id,
        "memory_id": memory_id
    })
    assert recall_result["title"] == "E2E Test Memory"
    assert recall_result["access_count"] == 1
    print(f"✓ Recalled memory with access_count={recall_result['access_count']}")
    
    # Search
    search_result = await plugin.call_tool("memory", {
        "operation": "search",
        "session_id": session_id,
        "query": "integration testing",
        "n_results": 5
    })
    assert "results" in search_result
    print(f"✓ Search returned {len(search_result['results'])} results")
    
    # List
    list_result = await plugin.call_tool("memory", {
        "operation": "list",
        "session_id": session_id,
        "limit": 10
    })
    assert list_result["total"] >= 1
    print(f"✓ List returned {list_result['total']} memories")
    
    # Delete
    delete_result = await plugin.call_tool("memory", {
        "operation": "delete",
        "session_id": session_id,
        "memory_id": memory_id
    })
    assert delete_result["deleted"] is True
    print(f"✓ Deleted memory: {memory_id}")
    
    # 7. Web Router
    print("\n[7] Web Interface")
    router = plugin.get_web_router()
    assert router is not None
    print(f"✓ Web router prefix: {router.prefix}")
    print(f"✓ Route count: {len(router.routes)}")
    
    # 8. Hook Integration
    print("\n[8] Hook Integration")
    assert hasattr(plugin, 'on_pre_llm_call')
    print("✓ Hook method exists: on_pre_llm_call")
    
    # 9. Schema Data
    print("\n[9] Schema Configuration")
    schema = plugin.get_schema_data()
    assert 'hooks' in schema or 'web_ui' in schema or 'tools' in schema
    print(f"✓ Schema loaded with keys: {list(schema.keys())}")
    
    print("\n" + "=" * 60)
    print("✅ ALL VALIDATIONS PASSED")
    print("=" * 60)
    
    return True


if __name__ == "__main__":
    try:
        result = asyncio.run(validate_e2e())
        sys.exit(0 if result else 1)
    except Exception as e:
        print(f"\n❌ VALIDATION FAILED: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
