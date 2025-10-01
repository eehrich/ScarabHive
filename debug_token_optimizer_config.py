"""Debug script to check token optimizer configuration."""
import asyncio
import yaml
from pathlib import Path
from agent_system.config.loader import load_config

async def main():
    config_path = Path("config/config.yaml")
    config = load_config(str(config_path))
    
    print("🔍 Token Optimizer Configuration Debug")
    print("=" * 60)
    
    # Check MCP default config
    if hasattr(config, 'mcp') and config.mcp:
        print("\n📦 MCP Config found")
        if hasattr(config.mcp, 'default_config') and config.mcp.default_config:
            dc = config.mcp.default_config
            print(f"  ├─ Has default_config: {dc is not None}")
            
            if hasattr(dc, 'agent_config') and dc.agent_config:
                ac = dc.agent_config
                print(f"  ├─ Has agent_config: {ac is not None}")
                
                if hasattr(ac, 'context_management') and ac.context_management:
                    cm = ac.context_management
                    print(f"  ├─ Has context_management: {cm is not None}")
                    
                    if hasattr(cm, 'token_optimization') and cm.token_optimization:
                        to = cm.token_optimization
                        print(f"  └─ Token Optimization Config:")
                        print(f"      ├─ enable_compression: {to.enable_compression}")
                        print(f"      ├─ compress_tool_results: {to.compress_tool_results}")
                        print(f"      ├─ cooldown_seconds: {getattr(to, 'cooldown_seconds', 'NOT SET')}")
                        print(f"      └─ min_token_increase: {getattr(to, 'min_token_increase', 'NOT SET')}")
                    else:
                        print("  └─ ❌ No token_optimization config!")
                else:
                    print("  └─ ❌ No context_management config!")
            else:
                print("  └─ ❌ No agent_config!")
        else:
            print("  └─ ❌ No default_config!")
    else:
        print("\n❌ No MCP config found!")
    
    # Also check raw YAML
    print("\n📄 Raw mcp.yaml token_optimization:")
    mcp_yaml_path = Path("config/mcp.yaml")
    if mcp_yaml_path.exists():
        with open(mcp_yaml_path) as f:
            mcp_data = yaml.safe_load(f)
            to_config = (mcp_data.get('default_config', {})
                        .get('agent_config', {})
                        .get('context_management', {})
                        .get('token_optimization', {}))
            print(f"  {yaml.dump(to_config, default_flow_style=False)}")
    else:
        print("  ❌ mcp.yaml not found!")

if __name__ == "__main__":
    asyncio.run(main())
