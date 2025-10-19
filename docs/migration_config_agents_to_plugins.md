# Migration Guide: Config Agents to Plugin-Based Agents

**Date:** 2025-01-19  
**Status:** Migration in Progress  
**Related Epic:** See backlog.md

## Executive Summary

The config-based agents system (`config/agents.yaml`) is being removed as it is redundant with the existing plugin system (`config/plugins.yaml`). Both systems provide identical functionality for creating zero-code agent instances with custom prompts, tool filtering, and LLM profiles.

**Migration Strategy:** Consolidate all agent definitions into `config/plugins.yaml` and enhance the plugin system with missing features (tool description overrides).

---

## Why This Change?

### Problem: Two Systems Doing the Same Thing

**`config/agents.yaml` (OLD):**
```yaml
agents:
  financial_analyst_agent:
    enabled: true
    base_type: basic_agent  # References plugin type
    agent_config:
      llm_profile: turbo
      system_template: "config/prompts/financial_analyst_prompt.yaml"
      tools:
        allowed: ["yahoo_finance/*"]
```

**`config/plugins.yaml` (EXISTING):**
```yaml
servers:
  web_research_agent:
    type: web_research_agent  # Plugin type
    enabled: true
    agent_config:
      llm_profile: chat
      tools:
        allowed: ["duckduckgo_search/*"]
```

### Both Systems Support:
- ✅ Zero-code agent creation (no Python required)
- ✅ Custom system prompts (inline or template)
- ✅ LLM profile selection
- ✅ Tool allow/block lists
- ✅ Max steps configuration
- ✅ Hook system configuration
- ✅ Metadata and visibility flags

### Redundancy Analysis:
- **Config agents**: Create agent instances from plugin types
- **Plugin servers**: Create agent instances from plugin types
- **Only difference**: File location (`agents.yaml` vs `plugins.yaml`)

---

## Migration Steps

### Step 1: Backup Your Configuration

```bash
# Backup current config files
cp config/agents.yaml config/agents.yaml.backup
cp config/plugins.yaml config/plugins.yaml.backup
```

### Step 2: Migrate Agent Definitions

For each agent in `config/agents.yaml`, create an equivalent entry in `config/plugins.yaml`:

**Before (`config/agents.yaml`):**
```yaml
agents:
  financial_analyst_agent:
    enabled: true
    description: "Professional financial analyst..."
    base_type: basic_agent
    
    agent_config:
      llm_profile: turbo
      max_steps: 20
      system_template: "config/prompts/financial_analyst_prompt.yaml"
      
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
        blocked:
          - "ssh_control/*"
      
      self_tool_descriptions:
        financial_analyst_agent_execute_task: "Analyze stocks and market data"
      
      hooks:
        enabled: true
    
    metadata:
      author: "AgentSystem"
      version: "1.0.0"
      tags: ["finance", "stocks"]
      category: "financial"
      visibility: "both"
```

**After (`config/plugins.yaml`):**
```yaml
servers:
  financial_analyst_agent:
    type: basic_agent  # base_type → type
    enabled: true
    description: "Professional financial analyst..."  # NEW: description field
    
    agent_config:
      llm_profile: turbo
      max_steps: 20
      system_template: "config/prompts/financial_analyst_prompt.yaml"
      
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
        blocked:
          - "ssh_control/*"
      
      self_tool_descriptions:  # NEW: tool override support
        financial_analyst_agent_execute_task: "Analyze stocks and market data"
      
      hooks:
        enabled: true
    
    # NEW: metadata field support
    metadata:
      author: "AgentSystem"
      version: "1.0.0"
      tags: ["finance", "stocks"]
      category: "financial"
      visibility: "both"
```

### Step 3: Field Mapping Reference

| Config Agents (OLD) | Plugin Servers (NEW) | Notes |
|---------------------|----------------------|-------|
| `agents:` | `servers:` | Top-level section |
| `base_type` | `type` | Plugin type to instantiate |
| `enabled` | `enabled` | Same |
| `description` | `description` | **NEW**: Add to plugin config |
| `agent_config.*` | `agent_config.*` | Same structure |
| `metadata` | `metadata` | **NEW**: Add to plugin config |

### Step 4: Update Configuration References

**Update `config/config.yaml`:**

```yaml
# BEFORE
includes:
  - llm.yaml
  - agents.yaml        # ← REMOVE
  - plugins.yaml
  - mcp_servers.yaml

# AFTER
includes:
  - llm.yaml
  - plugins.yaml       # All agents defined here now
  - mcp_servers.yaml
```

### Step 5: Remove Old Config File

```bash
# After successful migration and testing
rm config/agents.yaml
```

---

## Enhanced Plugin System Features

### NEW: `self_tool_descriptions` Support

Override descriptions for agent's own inherited tools:

```yaml
servers:
  my_agent:
    type: basic_agent
    agent_config:
      self_tool_descriptions:
        my_agent_execute_task: "Custom description for this agent's execute_task"
        my_agent_list_tools: "Custom description for this agent's list_tools"
```

**Validation:**
- Tool names must start with agent name prefix (`{agent_name}_`)
- Only applies to tools inherited from base plugin type
- Used in tool discovery and MCP tool schemas

### NEW: `metadata` Field Support

Add rich metadata to plugin instances:

```yaml
servers:
  my_agent:
    type: basic_agent
    metadata:
      author: "Your Name"
      version: "1.2.0"
      tags: ["research", "analysis"]
      category: "financial"
      visibility: "both"  # ui, tool, both, private
```

**Visibility Options:**
- `ui`: Visible in UI dropdown, NOT available as tool
- `tool`: Available as tool for other agents, NOT in UI
- `both`: Visible in UI AND available as tool
- `private`: Neither UI nor tool (testing/experimental)

### NEW: `description` Field Support

Add human-readable description to instances:

```yaml
servers:
  my_agent:
    type: basic_agent
    enabled: true
    description: "Detailed description of what this agent does"
```

---

## Complete Migration Example

### Before: Using `config/agents.yaml`

```yaml
# config/agents.yaml
agents:
  code_reviewer_agent:
    enabled: true
    description: "Expert code reviewer"
    base_type: basic_agent
    
    agent_config:
      llm_profile: chat
      max_steps: 15
      system_template: "config/prompts/code_reviewer_prompt.yaml"
      
      tools:
        allowed:
          - "script_interpreter/*"
          - "log_viewer/*"
        blocked:
          - "ssh_control/*"
      
      self_tool_descriptions:
        code_reviewer_agent_execute_task: "Review code quality and security"
    
    metadata:
      author: "DevOps Team"
      version: "1.1.0"
      tags: ["code", "review", "quality"]
      category: "development"
      visibility: "ui"
```

### After: Using `config/plugins.yaml`

```yaml
# config/plugins.yaml
servers:
  code_reviewer_agent:
    type: basic_agent  # base_type → type
    enabled: true
    description: "Expert code reviewer"
    
    agent_config:
      llm_profile: chat
      max_steps: 15
      system_template: "config/prompts/code_reviewer_prompt.yaml"
      
      tools:
        allowed:
          - "script_interpreter/*"
          - "log_viewer/*"
        blocked:
          - "ssh_control/*"
      
      self_tool_descriptions:
        code_reviewer_agent_execute_task: "Review code quality and security"
    
    metadata:
      author: "DevOps Team"
      version: "1.1.0"
      tags: ["code", "review", "quality"]
      category: "development"
      visibility: "ui"
```

---

## Testing Your Migration

### 1. Validate Configuration

```bash
# Check for syntax errors
python -m agent_system.config.settings

# List all configured servers
agent-cli plugins list
```

### 2. Test Agent Functionality

```bash
# Test agent execution
agent-cli run financial_analyst_agent "Analyze AAPL stock"

# Verify tool access
agent-cli plugins info financial_analyst_agent

# Check visibility
curl http://127.0.0.1:8000/agents
```

### 3. Run Test Suite

```bash
# Full test suite
python -m pytest -q

# Agent-specific tests
python -m pytest tests/test_agent*.py -v
```

---

## CLI Commands (No Changes)

All existing CLI commands continue to work:

```bash
# List agents (now from plugins.yaml)
agent-cli plugins list

# Show agent details
agent-cli plugins info <agent_name>

# Enable/disable agents
agent-cli plugins enable <agent_name>
agent-cli plugins disable <agent_name>

# Run agent
agent-cli run <agent_name> "Your task"
```

**Removed commands:**
```bash
# These config-agent commands are removed:
# agent-cli config-agents list
# agent-cli config-agents show <name>
# agent-cli config-agents validate
```

---

## API Endpoints (No Changes)

All existing API endpoints continue to work:

```bash
# List all agents
GET /agents

# Get agent details
GET /agents/{name}

# Execute task
POST /run?task=...&agent=<agent_name>

# Get allowed tools
GET /agents/{name}/allowed-tools
```

**Removed endpoints:**
```bash
# These config-agent endpoints are removed:
# GET /api/config-agents
# GET /api/config-agents/{name}
# POST /api/config-agents/validate
```

---

## Breaking Changes

### Removed Components

**Files Removed:**
- `config/agents.yaml` - Agent definitions moved to `plugins.yaml`
- `src/agent_system/plugins/config_agent_discovery.py`
- `src/agent_system/plugins/config_agent_factory.py`
- `src/agent_system/plugins/config_agent_validation.py`
- `docs/config_based_agents.md` - Merged into plugin authoring guide
- `docs/configurable_agents.md` - Design doc (historical)

**CLI Commands Removed:**
- `agent-cli config-agents list`
- `agent-cli config-agents show <name>`
- `agent-cli config-agents validate [name]`

**API Endpoints Removed:**
- `GET /api/config-agents`
- `GET /api/config-agents/{name}`
- `POST /api/config-agents/validate`

**Configuration Keys Removed:**
- `agents:` section (use `plugins.servers` instead)
- `config_agents:` in old `mcp.yaml` format

### Migration Required

If you have:
- ✅ Custom agents in `config/agents.yaml` → Migrate to `plugins.yaml`
- ✅ CLI scripts using `config-agents` commands → Use `plugins` commands
- ✅ API clients calling `/api/config-agents` → Use `/agents` instead
- ✅ References to `agents.yaml` in docs → Update to `plugins.yaml`

---

## Rollback Plan

If you need to rollback:

```bash
# 1. Restore backup
cp config/agents.yaml.backup config/agents.yaml
cp config/plugins.yaml.backup config/plugins.yaml

# 2. Re-add agents.yaml to includes
# Edit config/config.yaml:
includes:
  - agents.yaml  # Add back

# 3. Checkout old code version
git checkout <commit-before-migration>

# 4. Reinstall
pip install -e .
```

---

## Benefits of This Change

### Code Simplification
- ❌ Remove ~1500 lines of redundant code
- ❌ Remove duplicate discovery logic
- ❌ Remove duplicate validation code
- ❌ Remove duplicate CLI commands
- ❌ Remove duplicate API endpoints

### User Experience
- ✅ One configuration system to learn
- ✅ Consistent mental model (everything is a plugin)
- ✅ Same zero-code agent creation capability
- ✅ Enhanced plugin metadata support
- ✅ Tool description overrides for all instances

### Maintainability
- ✅ Single bootstrap process
- ✅ Less test surface area
- ✅ Clearer documentation
- ✅ Easier to extend and enhance

---

## FAQ

### Q: Will my existing agents.yaml work during migration?
**A:** Yes, temporarily. The old system will be deprecated but functional during the migration period. Update your config as soon as possible.

### Q: Can I create multiple instances of the same agent type?
**A:** Yes! Just like before:
```yaml
servers:
  financial_analyst_1:
    type: basic_agent
    agent_config: {...}
  
  financial_analyst_2:
    type: basic_agent
    agent_config: {...}  # Different config
```

### Q: Do I lose any functionality?
**A:** No. All features from config agents are now in plugin system:
- System prompts (template or inline)
- Tool filtering
- LLM profile selection
- Metadata and visibility
- Hook configuration
- **NEW:** Tool description overrides

### Q: What about the "zero-code" promise?
**A:** Still true! Creating agents in `plugins.yaml` requires zero Python code, just like `agents.yaml` did.

### Q: Can I still use plugin.yaml metadata?
**A:** Yes, `plugin.yaml` metadata is used for plugin-level info. Instance metadata in `plugins.yaml` overrides it per-instance.

### Q: How do I know if migration is complete?
**A:** When:
- ✅ `config/agents.yaml` is deleted
- ✅ All agents work from `plugins.yaml`
- ✅ Tests pass: `python -m pytest -q`
- ✅ No `config-agents` commands in scripts

---

## Support and Resources

**Documentation:**
- [Plugin Authoring Guide](plugin_authoring.md) - How to create plugins
- [MCP Configuration](mcp_configuration.md) - MCP server configuration
- [Plugin Hooks](plugin_hooks.md) - Hook system documentation

**Getting Help:**
- Check migration examples in this document
- Review `config/plugins.yaml` for configuration patterns
- Run validation: `agent-cli plugins list`
- Check logs: `logs/agent.log`

**Reporting Issues:**
- Check backlog.md for known issues
- Verify configuration syntax
- Test with minimal config first
- Include error logs when reporting

---

## Timeline

**Phase 1: Preparation (Current)**
- ✅ Create migration documentation
- ⏳ Add metadata support to plugin config model
- ⏳ Add self_tool_descriptions support to plugin loading
- ⏳ Add description field support

**Phase 2: Migration**
- ⏳ Migrate example agents to plugins.yaml
- ⏳ Update tests to use plugins.yaml
- ⏳ Update documentation references

**Phase 3: Cleanup**
- ⏳ Remove config agent discovery code
- ⏳ Remove config agent CLI commands
- ⏳ Remove config agent API endpoints
- ⏳ Remove agents.yaml from config
- ⏳ Remove deprecated documentation

**Phase 4: Validation**
- ⏳ Full test suite pass
- ⏳ Integration testing
- ⏳ Documentation review
- ⏳ User acceptance testing

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2025-01-19 | Initial migration guide created |

---

**Last Updated:** 2025-01-19  
**Next Review:** After Phase 2 completion
