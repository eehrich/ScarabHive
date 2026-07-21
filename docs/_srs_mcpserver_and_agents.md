# Software Requirements Specification (SRS) für MCP-Server und Agents

## 1. Einleitung

Das MCP (Model Context Protocol) Server-System bildet die Grundarchitektur des AgentSystems. Es definiert die Basis-Schnittstellen für alle Server-Implementierungen und spezialisiert sich in verschiedene Agent-Typen. Diese SRS beschreibt die Anforderungen an MCP-Server, Agents, Plugin-Agents und Configuration-based Agents.

## 2. Überblick über das MCP-Server-System

### 2.1 Zweck
Das MCP-Server-System bietet eine einheitliche Schnittstelle für Tool-basierte Operationen und ermöglicht verschiedene Spezialisierungen (Agents, Plugin-Agents, Config-Agents) mit gemeinsamer Basis-Funktionalität.

### 2.2 Server-Hierarchie

```
MCPServer (Basis-Interface)
    │
    ├── Agent (LLM-basierter Agent mit Tool-Execution)
    │   │
    │   ├── PluginAgent (Code-basierte Agents als Plugins)
    │   │
    │   └── ConfigAgent (YAML-konfigurierte Agents)
    │
    └── SchemaBasedMCPServer (Schema-definierte Server)

SchemaBasedMixin (Shared Functionality)
    │
    ├─► SchemaBasedMCPServer (nutzt Mixin für Schema-Loading)
    └─► SchemaBasedAgent (nutzt Mixin für Schema-Loading)
```

### 2.3 References (Architectures & Designs)
- [Plugin Architecture](docs/_sad_plugin_architecture.md)
- [Plugin System SRS](docs/_srs_pluginsystem.md)
- [Configurable Agents Design](docs/configurable_agents.md)
- [Tool Execution Flow](docs/tool_execution.md)
- [Status Messages Design](docs/status_design.md)
- [Hook System](docs/plugin_hooks.md)

## 3. Funktionale Anforderungen

### 3.1 MCP-Server Basis-Anforderungen

#### FR1: Tool Discovery
- **FR1.1**: Jeder MCP-Server muss Tools über `list_tools()` bereitstellen
- **FR1.2**: Tool-Schemas müssen MCPTool-Format entsprechen
- **FR1.3**: Tool-Metadaten müssen Name, Description und Input-Schema enthalten
- **FR1.4**: Tool-Listen müssen cachebar sein für Performance

#### FR2: Tool Execution
- **FR2.1**: Tools müssen über `call_tool(name, arguments)` ausführbar sein
- **FR2.2**: Tool-Calls müssen Request-IDs für Tracking unterstützen
- **FR2.3**: Tool-Execution muss Cancellation unterstützen
- **FR2.4**: Tool-Results müssen strukturierte Rückgabewerte liefern

#### FR3: Server Registration
- **FR3.1**: MCP-Server müssen in MCPRegistry registrierbar sein
- **FR3.2**: Server-Namen müssen eindeutig sein
- **FR3.3**: Server müssen Metadaten (name, description, version) bereitstellen
- **FR3.4**: Server müssen zur Laufzeit deregistrierbar sein

### 3.2 Agent-Anforderungen

#### FR4: LLM Integration
- **FR4.1**: Agents müssen LLM-Clients für Chat-Completions nutzen
- **FR4.2**: LLM-Profile müssen konfigurierbar sein (model, temperature, etc.)
- **FR4.3**: Agents müssen LLM-Override zur Laufzeit unterstützen
- **FR4.4**: Streaming-Responses müssen unterstützt werden

#### FR5: Tool Orchestration
- **FR5.1**: Agents müssen Tool-Calls basierend auf LLM-Responses ausführen
- **FR5.2**: Parallele Tool-Execution muss unterstützt werden
- **FR5.3**: Tool-Results müssen in Conversation-Context integriert werden
- **FR5.4**: Tool-Filtering (allowed/blocked) muss pro Agent konfigurierbar sein

#### FR6: Conversation Management
- **FR6.1**: Agents müssen Conversation-History verwalten (Sessions)
- **FR6.2**: Multi-Session Support muss gegeben sein
- **FR6.3**: Session-Messages müssen persistierbar sein
- **FR6.4**: Context-Optimization (Summarization) muss unterstützt werden

#### FR7: Execution Control
- **FR7.1**: Max-Steps Limit muss konfigurierbar sein
- **FR7.2**: Request-Cancellation muss async unterstützt werden
- **FR7.3**: Step-by-Step Execution mit Events muss möglich sein
- **FR7.4**: Error-Handling muss graceful degradation ermöglichen

### 3.3 Plugin-Agent-Anforderungen

#### FR8: Code-based Agents
- **FR8.1**: Plugin-Agents müssen als Python-Module in plugin_dirs liegen
- **FR8.2**: Plugin-Factory muss `(name, system_config, mcp_config)` Signature haben
- **FR8.3**: Custom Logic in `server.py` muss möglich sein
- **FR8.4**: Schema-Dateien (`schema.yaml`) müssen unterstützt werden
- **FR8.5**: SchemaBasedMixin ermöglicht automatisches Method-Routing von schema.yaml zu Python-Methoden

#### FR9: Plugin Metadata
- **FR9.1**: `plugin.yaml` muss Metadata (name, version, author) definieren
- **FR9.2**: Visibility-Flags (ui, tool, both, private) müssen unterstützt werden
- **FR9.3**: Dependencies zwischen Plugins müssen deklarierbar sein
- **FR9.4**: Hook-Integration muss via Schema definierbar sein

### 3.4 Config-Agent-Anforderungen

#### FR10: YAML-based Agents
- **FR10.1**: Agents müssen rein über YAML definierbar sein (ohne Python-Code)
- **FR10.2**: Config muss in `config.yaml` unter `agents` Section liegen
- **FR10.3**: System-Prompts müssen inline oder als Template-Referenz definierbar sein
- **FR10.4**: Tool-Filtering (allowed/blocked) muss konfigurierbar sein

#### FR11: Config Validation
- **FR11.1**: Config-Agent Definitionen müssen zur Load-Time validiert werden
- **FR11.2**: Fehlende LLM-Profile müssen Fehler werfen
- **FR11.3**: Invalid Tool-Patterns müssen erkannt werden
- **FR11.4**: Template-Pfade müssen auf Existenz geprüft werden

### 3.5 Tool Execution & Cancellation

#### FR12: Execution Flow
- **FR12.1**: Tool-Calls müssen über Registry zu Server geroutet werden
- **FR12.2**: Internal Tools (Agent-eigene) müssen höchste Priorität haben
- **FR12.3**: Status-Updates müssen während Execution gesendet werden
- **FR12.4**: Tool-Errors müssen strukturiert an LLM zurückgegeben werden

#### FR13: Cancellation
- **FR13.1**: Laufende Requests müssen über Request-ID abbrechbar sein
- **FR13.2**: Cancellation muss async ohne Deadlocks funktionieren
- **FR13.3**: Partial Results bei Cancellation müssen zurückgegeben werden
- **FR13.4**: Cleanup nach Cancellation muss garantiert sein

## 4. Nicht-funktionale Anforderungen

### 4.1 Performance
- **NFR1.1**: Tool-Discovery darf max. 100ms dauern (mit Caching)
- **NFR1.2**: Tool-Execution Start-Overhead < 50ms
- **NFR1.3**: Session-Loading < 200ms für 1000 Messages
- **NFR1.4**: Parallele Tool-Calls müssen effizient skalieren

### 4.2 Reliability
- **NFR2.1**: LLM-Failures dürfen Agent nicht crashen (graceful degradation)
- **NFR2.2**: Tool-Execution-Errors müssen isoliert behandelt werden
- **NFR2.3**: Session-Corruption muss verhindert werden
- **NFR2.4**: Registry-Inkonsistenzen müssen erkannt werden

### 4.3 Maintainability
- **NFR3.1**: Agent-Code muss erweiterbar ohne Core-Änderungen sein
- **NFR3.2**: Logging muss strukturiert und filterbar sein
- **NFR3.3**: Testing muss ohne externe Services möglich sein (Mocks)
- **NFR3.4**: Konfiguration muss validierbar und dokumentiert sein

### 4.4 Security
- **NFR4.1**: Tool-Access muss per Agent konfigurierbar sein (allowed/blocked)
- **NFR4.2**: LLM-API-Keys müssen sicher gespeichert werden
- **NFR4.3**: User-Input muss sanitized werden
- **NFR4.4**: Tool-Execution muss in isolierten Kontexten erfolgen

## 5. Detaillierte Architektur

### 5.1 Klassendiagramm

```python
# Basis MCP-Server Interface
class MCPServer:
    """Basis-Interface für alle MCP-Server."""
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
    
    async def list_tools(self) -> List[MCPTool]:
        """List all available tools."""
        raise NotImplementedError
    
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool call."""
        raise NotImplementedError


# Agent Basis-Klasse
class Agent(MCPServer):
    """LLM-basierter Agent mit Tool-Execution."""
    def __init__(
        self, 
        name: str, 
        system_config: AgentSystemConfig, 
        mcp_config: MCPConfig, 
        registry: MCPRegistry,
        llm: Optional[LLMClient] = None,
        llm_factory: Optional[Callable] = None
    ):
        super().__init__(name, system_config, mcp_config)
        self.registry = registry
        self.llm = llm or self._create_llm(llm_factory)
        self.agent_config = mcp_config.agent_config
        self._sessions: Dict[str, List[Message]] = {}
        self._request_lock = asyncio.Lock()
        self._active_requests: Dict[str, asyncio.Event] = {}
    
    async def run_events(
        self, 
        task: str, 
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        llm: Optional[LLMClient] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """Execute task with streaming events."""
        # Implementation...
    
    async def execute_tool_calls(
        self, 
        tool_calls: List[ToolCall],
        request_id: str,
        status_callback: Optional[Callable] = None
    ) -> List[ToolResult]:
        """Execute multiple tool calls (parallel if possible)."""
        # Implementation...
    
    async def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request."""
        # Implementation...
    
    async def optimize_context(self, session_id: str) -> None:
        """Optimize conversation context (via hooks)."""
        # Delegated to context_optimizer hook plugin


# Plugin-Agent (Code-based)
class PluginAgent(Agent):
    """Agent defined as plugin with custom Python code."""
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig, registry: MCPRegistry):
        super().__init__(name, system_config, mcp_config, registry)
        # Plugin-specific initialization
        self._plugin_metadata = self._load_plugin_metadata()
    
    def _load_plugin_metadata(self) -> Dict[str, Any]:
        """Load plugin.yaml metadata."""
        # Implementation...


# Config-Agent (YAML-based)
class ConfigAgent(Agent):
    """Agent defined purely via YAML configuration."""
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig, registry: MCPRegistry):
        super().__init__(name, system_config, mcp_config, registry)
        # Config-specific initialization from agents.yaml
        self._config_definition = self._load_config_definition()
    
    def _load_config_definition(self) -> ConfigBasedAgentDefinition:
        """Load agent definition from config."""
        # Implementation...
```

### 5.2 MCPRegistry Design

```python
class MCPRegistry:
    """Central registry for all MCP servers."""
    def __init__(self):
        self._servers: Dict[str, MCPServer] = {}
        self._lock = threading.RLock()
    
    def register(self, name: str, server: MCPServer) -> None:
        """Register an MCP server."""
        with self._lock:
            if name in self._servers:
                raise ValueError(f"Server '{name}' already registered")
            self._servers[name] = server
    
    def get(self, name: str) -> MCPServer:
        """Get a registered server."""
        with self._lock:
            if name not in self._servers:
                raise KeyError(f"Server '{name}' not found")
            return self._servers[name]
    
    def list(self) -> List[str]:
        """List all registered server names."""
        with self._lock:
            return list(self._servers.keys())
    
    def unregister(self, name: str) -> None:
        """Unregister a server."""
        with self._lock:
            if name in self._servers:
                del self._servers[name]
```

### 5.3 Tool Execution Flow

```
User Request
    │
    ├─► Agent.run_events(task)
    │   │
    │   ├─► Load/Create Session
    │   ├─► Build System Prompt (with context injection)
    │   ├─► Execute Hook: pre_request
    │   │
    │   └─► Execution Loop (max_steps)
    │       │
    │       ├─► LLM Chat Completion (with tools)
    │       ├─► Parse Tool Calls from Response
    │       │
    │       ├─► Execute Tool Calls (parallel)
    │       │   │
    │       │   ├─► Route to Registry
    │       │   ├─► Call Server.call_tool()
    │       │   ├─► Send Status Updates
    │       │   └─► Return Results
    │       │
    │       ├─► Append Results to Conversation
    │       ├─► Execute Hook: post_tool_execution
    │       │
    │       └─► Check Completion or Continue
    │
    └─► Execute Hook: post_request
    └─► Return Final Result
```

### 5.4 Configuration Structure

#### 5.4.1 Plugin-Agent Configuration

```yaml
# config/mcp.yaml
plugins:
  plugin_dirs:
    - src/plugins
  
  servers:
    my_research_agent:
      type: research_agent  # Plugin type
      enabled: true
      agent_config:
        llm_profile: turbo
        max_steps: 20
        tools:
          allowed:
            - "duckduckgo_search/*"
            - "web_scraper/*"
          blocked:
            - "ssh_control/*"
```

```yaml
# src/plugins/research_agent/plugin.yaml
name: research_agent
version: 1.0.0
author: AgentSystem Team
description: "Web research and fact-checking agent"
visibility: ui  # ui, tool, both, private
category: research
tags:
  - web
  - research
  - fact-checking

dependencies:
  - duckduckgo_search
  - web_scraper
```

#### 5.4.2 Config-Agent Configuration

```yaml
# config/config.yaml
agents:
  financial_analyst:
    enabled: true
    description: "Financial analysis and stock research agent"
    base_type: agent
    
    agent_config:
      llm_profile: turbo
      max_steps: 15
      system_template: "config/prompts/financial_analyst_prompt.md"
      
      tools:
        allowed:
          - "yahoo_finance/*"
          - "web_scraper/*"
        blocked:
          - "ssh_control/*"
      
      hooks:
        enabled: true
        enabled_hooks:
          - context_optimizer
          - context_summarizer
    
    metadata:
      author: "Finance Team"
      version: "1.0.0"
      tags: ["finance", "research"]
      visibility: ui
```

### 5.5 Tool Discovery & Execution

#### 5.5.1 Tool Discovery Process

```python
async def discover_tools(agent: Agent) -> List[MCPTool]:
    """Discover all available tools for an agent."""
    all_tools = []
    
    # 1. Get tools from registry servers
    for server_name in agent.registry.list():
        server = agent.registry.get(server_name)
        try:
            server_tools = await server.list_tools()
            all_tools.extend(server_tools)
        except Exception as e:
            logger.error(f"Failed to list tools from {server_name}: {e}")
    
    # 2. Apply tool filtering (allowed/blocked)
    filtered_tools = agent._filter_tools(all_tools)
    
    # 3. Add internal tools (self-description, etc.)
    internal_tools = agent._get_internal_tools()
    filtered_tools.extend(internal_tools)
    
    # 4. Apply custom tool descriptions (if configured)
    if agent.agent_config.self_tool_descriptions:
        filtered_tools = agent._apply_custom_descriptions(filtered_tools)
    
    return filtered_tools
```

#### 5.5.2 Tool Execution with Status

```python
async def execute_tool_with_status(
    agent: Agent,
    tool_call: ToolCall,
    request_id: str,
    status_callback: Optional[Callable] = None
) -> ToolResult:
    """Execute a single tool call with status updates."""
    try:
        # Send status: start
        if status_callback:
            await status_callback({
                "type": "tool_status",
                "tool": tool_call.name,
                "status": "running",
                "request_id": request_id
            })
        
        # Route to appropriate server
        if tool_call.name in agent._internal_tools:
            # Execute internal tool
            result = await agent._execute_internal_tool(tool_call)
        else:
            # Route to registry
            server_name = agent._resolve_server_for_tool(tool_call.name)
            server = agent.registry.get(server_name)
            
            # Execute on server
            result = await server.call_tool(tool_call.name, tool_call.arguments)
        
        # Send status: success
        if status_callback:
            await status_callback({
                "type": "tool_status",
                "tool": tool_call.name,
                "status": "completed",
                "request_id": request_id
            })
        
        return ToolResult(success=True, result=result)
        
    except Exception as e:
        # Send status: error
        if status_callback:
            await status_callback({
                "type": "tool_status",
                "tool": tool_call.name,
                "status": "failed",
                "error": str(e),
                "request_id": request_id
            })
        
        return ToolResult(success=False, error=str(e))
```

### 5.6 Cancellation Mechanism

```python
class Agent(MCPServer):
    async def run_events(self, task: str, request_id: str, ...) -> AsyncIterator[Dict]:
        """Execute task with cancellation support."""
        # Register cancellation event
        cancel_event = asyncio.Event()
        self._active_requests[request_id] = cancel_event
        
        try:
            async with self._request_lock:
                # Execution loop
                for step in range(self.agent_config.max_steps):
                    # Check for cancellation
                    if cancel_event.is_set():
                        yield {"type": "cancelled", "step": step}
                        break
                    
                    # Execute step...
                    # ...
                    
        finally:
            # Cleanup
            del self._active_requests[request_id]
    
    async def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request."""
        if request_id in self._active_requests:
            self._active_requests[request_id].set()
            return True
        return False
```

### 5.7 Hook Integration

```python
# Agent uses hooks for extensibility
class Agent(MCPServer):
    async def _execute_hooks(self, hook_type: str, context: Dict[str, Any]) -> None:
        """Execute registered hooks for a specific type."""
        # Get hook plugins from registry
        hook_plugins = [
            server for server in self.registry._servers.values()
            if hasattr(server, 'get_hooks') and hook_type in server.get_hooks()
        ]
        
        # Execute hooks sequentially
        for plugin in hook_plugins:
            try:
                hook_func = plugin.get_hooks()[hook_type]
                await hook_func(context)
            except Exception as e:
                logger.error(f"Hook {hook_type} failed in {plugin.name}: {e}")
    
    async def run_events(self, task: str, ...) -> AsyncIterator[Dict]:
        """Execute with hooks."""
        # Pre-request hook
        await self._execute_hooks("pre_request", {"task": task, "agent": self})
        
        # ... execution ...
        
        # Post-tool hook
        await self._execute_hooks("post_tool_execution", {
            "tool_calls": tool_calls,
            "results": results,
            "agent": self
        })
        
        # Post-request hook
        await self._execute_hooks("post_request", {
            "task": task,
            "result": final_result,
            "agent": self
        })
```

## 6. Schnittstellen-Definitionen

### 6.1 MCPServer Interface

```python
class MCPServer:
    """Basis-Interface für alle MCP-Server."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig):
        """Initialize MCP server.
        
        Args:
            name: Server name (unique identifier)
            system_config: System-wide configuration
            mcp_config: Server-specific MCP configuration
        """
        pass
    
    async def list_tools(self) -> List[MCPTool]:
        """List all tools provided by this server.
        
        Returns:
            List of MCPTool objects with schemas
        """
        raise NotImplementedError
    
    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool call.
        
        Args:
            name: Tool name
            arguments: Tool arguments (validated against schema)
        
        Returns:
            Tool execution result
        
        Raises:
            ToolNotFoundException: If tool not found
            ToolExecutionError: If execution fails
        """
        raise NotImplementedError
    
    @property
    def description(self) -> str:
        """Human-readable server description."""
        return f"MCP Server: {self.name}"
```

### 6.2 Agent Interface

```python
class Agent(MCPServer):
    """LLM-based agent interface."""
    
    async def run_events(
        self,
        task: str,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        llm: Optional[LLMClient] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        """Execute task with streaming events.
        
        Args:
            task: Task description
            session_id: Optional session ID for context
            request_id: Optional request ID for tracking
            llm: Optional LLM override
        
        Yields:
            Event dicts: {type, data, ...}
        """
        pass
    
    async def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request.
        
        Args:
            request_id: Request to cancel
        
        Returns:
            True if cancelled, False if not found
        """
        pass
    
    async def create_session(self) -> str:
        """Create a new session.
        
        Returns:
            Session ID
        """
        pass
    
    async def get_session(self, session_id: str) -> Optional[List[Message]]:
        """Get session messages.
        
        Args:
            session_id: Session ID
        
        Returns:
            List of messages or None
        """
        pass
    
    async def optimize_context(self, session_id: str) -> None:
        """Optimize conversation context.
        
        Args:
            session_id: Session to optimize
        """
        pass
```

### 6.3 MCPRegistry Interface

```python
class MCPRegistry:
    """Registry for MCP servers."""
    
    def register(self, name: str, server: MCPServer) -> None:
        """Register a server.
        
        Args:
            name: Unique server name
            server: MCPServer instance
        
        Raises:
            ValueError: If name already registered
        """
        pass
    
    def get(self, name: str) -> MCPServer:
        """Get a registered server.
        
        Args:
            name: Server name
        
        Returns:
            MCPServer instance
        
        Raises:
            KeyError: If server not found
        """
        pass
    
    def list(self) -> List[str]:
        """List all registered server names."""
        pass
    
    def unregister(self, name: str) -> None:
        """Unregister a server."""
        pass
```

### 6.4 Plugin Factory Interface

```python
def plugin_factory(
    name: str,
    system_config: AgentSystemConfig,
    mcp_config: MCPConfig
) -> MCPServer:
    """Standard plugin factory signature.
    
    Args:
        name: Instance name
        system_config: System-wide configuration
        mcp_config: Plugin-specific MCP configuration
    
    Returns:
        MCPServer instance (Agent, PluginAgent, etc.)
    """
    pass
```

### 6.5 Config Agent Discovery Interface

```python
def discover_config_agents(
    agents_config: Optional[Dict[str, ConfigBasedAgentDefinition]]
) -> Dict[str, Callable]:
    """Discover config-based agents.
    
    Args:
        agents_config: Dict of agent definitions from config
    
    Returns:
        Dict mapping agent_name -> factory_function
    """
    pass
```

## 7. Bootstrap & Registration Process

### 7.1 Bootstrap Flow

```python
def bootstrap_servers(config: AgentSystemConfig, registry: MCPRegistry) -> None:
    """Bootstrap all MCP servers and agents."""
    
    # 1. Discover plugin-based servers
    plugin_dirs = config.plugins.plugin_dirs if config.plugins else []
    plugins = discover_all_plugins(dirs=plugin_dirs)
    
    # 2. Discover config-based agents
    config_agents = discover_config_agents(config.agents)
    
    # 3. Merge (config agents override plugins with same name)
    all_factories = {**plugins, **config_agents}
    
    # 4. Instantiate enabled servers
    enabled_servers = []
    
    # From plugins.servers
    if config.plugins:
        enabled_servers.extend([
            k for k, v in config.plugins.servers.items() if v.enabled
        ])
    
    # From agents (config-based)
    if config_agents:
        enabled_servers.extend(config_agents.keys())
    
    # 5. Create instances
    for server_name in enabled_servers:
        # Get MCP config
        if server_name in config_agents:
            # Config agent
            server_mcp_cfg = create_mcp_config_from_agent_def(
                config.agents[server_name]
            )
        else:
            # Plugin
            server_mcp_cfg = get_mcp_config_by_name(server_name, config)
        
        # Get factory
        factory_type = server_mcp_cfg.type
        factory = all_factories.get(factory_type)
        
        if factory:
            # Create instance
            instance = factory(server_name, config, server_mcp_cfg)
            
            # Register
            registry.register(server_name, instance)
            
            # Post-registration setup
            if isinstance(instance, Agent):
                instance.registry = registry  # Inject shared registry
                setup_agent_visibility(instance, factory)
```

### 7.2 Agent Visibility Configuration

```python
def setup_agent_visibility(agent: Agent, factory: Callable) -> None:
    """Configure agent visibility based on metadata."""
    metadata = getattr(factory, '_plugin_metadata', {})
    visibility = metadata.get('visibility', 'private')
    
    # Map visibility to flags
    if visibility == 'ui':
        agent._mcp_public = True
        agent._mcp_tool_visible = False
    elif visibility == 'tool':
        agent._mcp_public = False
        agent._mcp_tool_visible = True
    elif visibility == 'both':
        agent._mcp_public = True
        agent._mcp_tool_visible = True
    else:  # 'private'
        agent._mcp_public = False
        agent._mcp_tool_visible = False
```

## 8. Test-Anforderungen

### 8.1 Unit Tests

- **MCP Server Tests**:
  - Tool discovery (list_tools)
  - Tool execution (call_tool)
  - Error handling
  
- **Agent Tests**:
  - LLM integration
  - Tool orchestration
  - Session management
  - Cancellation
  
- **Registry Tests**:
  - Registration/deregistration
  - Concurrent access
  - Error cases

### 8.2 Integration Tests

- **End-to-End Workflows**:
  - Plugin agent execution
  - Config agent execution
  - Multi-agent collaboration
  
- **Tool Execution**:
  - Parallel execution
  - Error propagation
  - Status updates

### 8.3 Performance Tests

- Tool discovery latency
- Execution overhead
- Session loading speed
- Cancellation responsiveness

## 9. Security & Compliance

### 9.1 Tool Access Control

- Tool filtering per agent (allowed/blocked)
- Sandboxed execution contexts
- API key management

### 9.2 Input Validation

- Tool argument validation against schemas
- SQL injection prevention
- Path traversal protection

### 9.3 Audit Logging

- All tool executions logged
- Agent creation/deletion tracked
- Configuration changes recorded

---

Diese SRS definiert die vollständige Architektur des MCP-Server- und Agent-Systems im AgentSystem. Sie stellt sicher, dass alle Komponenten konsistent, erweiterbar und wartbar sind.
