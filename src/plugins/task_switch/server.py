"""Task Switch Plugin - State machine control for LLM agents.

This plugin allows agents to manage their own task state, enabling
dynamic prompt switching based on the current operational mode.

The plugin modifies the agent's `template_vars` at runtime, making the
current task available in Jinja2 prompts as {{ current_task }}.

Configuration in agent YAML:
```yaml
my_agent:
  agent_config:
    template_vars:
      current_task: "init"  # Initial task state
    tools:
      allowed:
        - "task_switch/*"
```

Plugin configuration in plugins.yaml:
```yaml
plugins:
  servers:
    task_switch:
      type: task_switch
      enabled: true
      config:
        task_var_name: "current_task"  # Variable name in template_vars
        allowed_tasks:  # Optional: restrict valid task states
          - init
          - analyze
          - execute
          - review
```

Example prompt usage:
```jinja2
{% if current_task == 'analyze' %}
Focus on analyzing the problem.
{% elif current_task == 'execute' %}
Execute the plan step by step.
{% endif %}
```
"""

from typing import Any, Dict, List, Optional, TYPE_CHECKING
import logging

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class TaskSwitchServer(SchemaBasedMCPServer):
    """Task state management server."""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)
        
        # Config for variable name and allowed tasks
        self._task_var_name = "current_task"
        self._allowed_tasks: Optional[List[str]] = None
        
        config_dict = mcp_config.config if hasattr(mcp_config, "config") else {}
        if config_dict:
            self._task_var_name = config_dict.get("task_var_name", "current_task")
            allowed = config_dict.get("allowed_tasks")
            if allowed and isinstance(allowed, list):
                self._allowed_tasks = [str(t) for t in allowed]
    
    def get_template_vars(self) -> Dict[str, Any]:
        """Provide template variables for schema rendering."""
        base_vars = super().get_template_vars()
        base_vars["allowed_tasks"] = self._allowed_tasks or []
        return base_vars
    
    async def set_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set the current task state."""
        status = params.get("_status")
        task_name = params.get("task_name", "").strip()
        agent = params.get("_agent")
        
        if not task_name:
            return {"status": "error", "error": "task_name is required"}
        
        # Validate task is allowed (if restrictions configured)
        if self._allowed_tasks and task_name not in self._allowed_tasks:
            return {
                "status": "error",
                "error": f"Invalid task '{task_name}'. Allowed tasks: {self._allowed_tasks}"
            }
        
        # Get agent_config from agent instance
        agent_config = agent.agent_config if agent and hasattr(agent, 'agent_config') else None
        
        # Get previous task
        previous_task = "init"
        if agent_config and hasattr(agent_config, 'template_vars'):
            if agent_config.template_vars is None:
                agent_config.template_vars = {}
            previous_task = agent_config.template_vars.get(self._task_var_name, "init")
            agent_config.template_vars[self._task_var_name] = task_name
            logger.debug(f"Task: {previous_task} -> {task_name}")
        else:
            logger.warning("No agent_config - task switch won't affect prompts")
        
        if status:
            await status.progress(f"Task: {previous_task} -> {task_name}")
        
        return {"status": "success", "previous_task": previous_task, "current_task": task_name}
