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
        
        # NEW: Preconditions per task (gate checks)
        task_preconditions:
          execute:
            tool: "some_validation_tool"
            params:
              book_id: "{{ book_id }}"  # Jinja2 template from agent's template_vars
            gate_field: "can_proceed"   # Field in result that must be true
            error_field: "gate_reason"  # Field with error message
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
import re

from agent_system.mcp.schema_based import SchemaBasedMCPServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class TaskSwitchServer(SchemaBasedMCPServer):
    """Task state management server with optional precondition gates."""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)
        
        # Config for variable name and allowed tasks
        self._task_var_name = "current_task"
        self._allowed_tasks: Optional[List[str]] = None
        self._task_preconditions: Dict[str, Dict[str, Any]] = {}
        
        config_dict = mcp_config.config if hasattr(mcp_config, "config") else {}
        if config_dict:
            self._task_var_name = config_dict.get("task_var_name", "current_task")
            allowed = config_dict.get("allowed_tasks")
            if allowed and isinstance(allowed, list):
                self._allowed_tasks = [str(t) for t in allowed]
            
            # Load task preconditions
            preconditions = config_dict.get("task_preconditions", {})
            if isinstance(preconditions, dict):
                self._task_preconditions = preconditions
    
    def get_template_vars(self) -> Dict[str, Any]:
        """Provide template variables for schema rendering."""
        base_vars = super().get_template_vars()
        base_vars["allowed_tasks"] = self._allowed_tasks or []
        base_vars["has_preconditions"] = bool(self._task_preconditions)
        return base_vars
    
    def _render_template_value(self, value: Any, template_vars: Dict[str, Any]) -> Any:
        """Render Jinja2-style {{ var }} templates in values."""
        if isinstance(value, str):
            # Simple template replacement: {{ var_name }}
            pattern = r'\{\{\s*(\w+)\s*\}\}'
            
            def replace_var(match: re.Match) -> str:
                var_name = match.group(1)
                var_value = template_vars.get(var_name, match.group(0))
                return str(var_value) if var_value is not None else match.group(0)
            
            return re.sub(pattern, replace_var, value)
        elif isinstance(value, dict):
            return {k: self._render_template_value(v, template_vars) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._render_template_value(item, template_vars) for item in value]
        return value
    
    def _get_nested_value(self, data: Dict[str, Any], field_path: str) -> Any:
        """Get nested value from dict using dot notation (e.g., 'summary.can_proceed')."""
        parts = field_path.split('.')
        current = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current
    
    async def _check_precondition(self, task_name: str, agent: Any, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Check precondition for a task. Returns gate status."""
        precondition = self._task_preconditions.get(task_name)
        if not precondition:
            return {"gate_open": True}
        
        tool_name = precondition.get("tool")
        if not tool_name:
            logger.warning(f"Precondition for task '{task_name}' missing 'tool' field")
            return {"gate_open": True}
        
        # Get template vars from agent config
        template_vars = {}
        if agent and hasattr(agent, 'agent_config') and agent.agent_config:
            template_vars = dict(agent.agent_config.template_vars or {})
        
        # Also include session metadata if available (for runtime vars like book_id)
        session_id = params.get("_session_id") if params else None
        if session_id and agent and hasattr(agent, '_session_service') and agent._session_service:
            try:
                session = await agent._session_service.get(session_id)
                if session and session.metadata:
                    # Session metadata takes precedence over static config
                    template_vars.update(session.metadata)
                    logger.debug(f"Added session metadata to template_vars: {list(session.metadata.keys())}")
            except Exception as e:
                logger.debug(f"Could not load session metadata: {e}")
        
        # Render params with template vars
        raw_params = precondition.get("params", {})
        rendered_params = self._render_template_value(raw_params, template_vars)
        
        # Call the validation tool
        try:
            logger.debug(f"Checking precondition for task '{task_name}': {tool_name}({rendered_params})")
            result = await agent.call_tool(tool_name, rendered_params)
            
            if not isinstance(result, dict):
                logger.warning(f"Precondition tool '{tool_name}' returned non-dict: {type(result)}")
                return {"gate_open": True, "warning": "Precondition check returned unexpected type"}
            
            # Check gate field
            gate_field = precondition.get("gate_field", "can_proceed")
            gate_value = self._get_nested_value(result, gate_field)
            
            if gate_value is True:
                return {"gate_open": True, "precondition_result": result}
            
            # Gate closed - get error message
            error_field = precondition.get("error_field", "gate_reason")
            error_message = self._get_nested_value(result, error_field)
            
            if not error_message:
                error_message = f"Gate '{gate_field}' is not true for task '{task_name}'"
            
            return {
                "gate_open": False,
                "gate_field": gate_field,
                "gate_value": gate_value,
                "reason": error_message,
                "precondition_result": result
            }
            
        except Exception as e:
            logger.error(f"Precondition check failed for task '{task_name}': {e}")
            return {
                "gate_open": False,
                "reason": f"Precondition check failed: {str(e)}",
                "error": str(e)
            }
    
    async def set_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set the current task state (with optional precondition check)."""
        status = params.get("_status")
        task_name = params.get("task_name", "").strip()
        agent = params.get("_agent")
        session_id = params.get("_session_id")
        
        if not task_name:
            return {"status": "error", "error": "task_name is required"}
        
        # Validate task is allowed (if restrictions configured)
        if self._allowed_tasks and task_name not in self._allowed_tasks:
            return {
                "status": "error",
                "error": f"Invalid task '{task_name}'. Allowed tasks: {self._allowed_tasks}"
            }
        
        # Check precondition (gate check)
        if task_name in self._task_preconditions and agent:
            gate_result = await self._check_precondition(task_name, agent, params)
            
            if not gate_result.get("gate_open", True):
                if status:
                    await status.error(f"Gate closed for task '{task_name}': {gate_result.get('reason', 'Unknown')}")
                
                return {
                    "status": "blocked",
                    "task_name": task_name,
                    "gate_closed": True,
                    "reason": gate_result.get("reason", "Precondition not met"),
                    "gate_field": gate_result.get("gate_field"),
                    "gate_value": gate_result.get("gate_value"),
                    "precondition_result": gate_result.get("precondition_result")
                }
        
        # Get previous task from SESSION-SCOPED template vars (not agent_config!)
        # CRITICAL: This ensures session isolation - multiple sessions won't contaminate each other
        previous_task = "init"
        if session_id and agent and hasattr(agent, '_session_tracker') and agent._session_tracker:
            session_vars = agent._session_tracker.get_session_template_vars(session_id)
            previous_task = session_vars.get(self._task_var_name, "init")
            # Update session-scoped template vars
            agent._session_tracker.set_session_template_vars(session_id, {self._task_var_name: task_name})
            logger.debug(f"Task: {previous_task} -> {task_name} (session-scoped)")
        else:
            # Fallback: Try agent_config (for backwards compatibility, but log warning)
            agent_config = agent.agent_config if agent and hasattr(agent, 'agent_config') else None
            if agent_config and hasattr(agent_config, 'template_vars'):
                if agent_config.template_vars is None:
                    agent_config.template_vars = {}
                previous_task = agent_config.template_vars.get(self._task_var_name, "init")
                agent_config.template_vars[self._task_var_name] = task_name
                logger.warning(f"Task switch using agent_config (no session isolation!): {previous_task} -> {task_name}")
            else:
                logger.warning("No session_tracker or agent_config - task switch won't affect prompts")
        
        # Persist to session storage (for session reload)
        persisted = False
        if session_id:
            await self._persist_context_vars(agent, session_id, {self._task_var_name: task_name})
            persisted = True
        
        if status:
            await status.end(f"Task: {previous_task} → {task_name}")
        
        return {"status": "success", "previous_task": previous_task, "current_task": task_name, "persisted": persisted}

    async def set_context(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set runtime context variables (template_vars) for the agent.
        
        This allows setting values like book_id at runtime, which can then be
        used in precondition checks and prompt templates.
        
        Args:
            params: Dict containing key-value pairs to set in template_vars
                   Special keys starting with _ are ignored (internal params)
        """
        status = params.get("_status")
        agent = params.get("_agent")
        session_id = params.get("_session_id")
        
        # Extract user-provided context vars (ignore internal _ params)
        context_vars = {k: v for k, v in params.items() if not k.startswith('_')}
        
        if not context_vars:
            if status:
                await status.error("No context variables provided")
            return {"status": "error", "error": "No context variables provided"}
        
        # Get previous values from SESSION-SCOPED template vars (not agent_config!)
        # CRITICAL: This ensures session isolation - multiple sessions won't contaminate each other
        previous_values = {}
        
        if session_id and agent and hasattr(agent, '_session_tracker') and agent._session_tracker:
            session_vars = agent._session_tracker.get_session_template_vars(session_id)
            for key in context_vars:
                previous_values[key] = session_vars.get(key)
            # Update session-scoped template vars
            agent._session_tracker.set_session_template_vars(session_id, context_vars)
            logger.debug(f"Set context (session-scoped): {list(context_vars.keys())}")
        else:
            # Fallback: Try agent_config (for backwards compatibility, but log warning)
            if agent and hasattr(agent, 'agent_config') and agent.agent_config:
                if agent.agent_config.template_vars is None:
                    agent.agent_config.template_vars = {}
                for key, value in context_vars.items():
                    previous_values[key] = agent.agent_config.template_vars.get(key)
                    agent.agent_config.template_vars[key] = value
                logger.warning(f"Set context using agent_config (no session isolation!): {list(context_vars.keys())}")
            else:
                if status:
                    await status.error("No session_tracker or agent_config available")
                return {"status": "error", "error": "No session_tracker or agent_config available"}
        
        # Persist to session storage (for session reload)
        persisted = False
        if session_id:
            await self._persist_context_vars(agent, session_id, context_vars)
            persisted = True
        
        # Build concise status message
        if status:
            var_items = list(context_vars.items())
            if len(var_items) <= 2:
                var_summary = ", ".join(f"{k}={v}" for k, v in var_items)
            else:
                first_two = ", ".join(f"{k}={v}" for k, v in var_items[:2])
                var_summary = f"{first_two} +{len(var_items)-2}"
            await status.end(f"Set: {var_summary}")
        
        return {
            "status": "success",
            "updated": context_vars,
            "previous": previous_values,
            "persisted": persisted
        }

    async def _persist_context_vars(self, agent: Any, session_id: str, vars_to_update: Dict[str, Any]) -> None:
        """Persist context variables to session storage.
        
        This ensures template_vars survive server restarts and session reloads.
        """
        if not agent or not hasattr(agent, '_session_service') or not agent._session_service:
            logger.debug("No session service - context vars not persisted")
            return
        
        try:
            session_manager = agent._session_service.session_manager
            if not session_manager:
                return
            
            # Get user_id from session tracker
            user_id = "anonymous"
            if hasattr(agent, '_session_tracker'):
                metadata = agent._session_tracker.get_session_metadata(session_id)
                if metadata:
                    user_id = metadata.get("user_id", "anonymous")
            
            # Load current session
            session_data = await session_manager.load_session(user_id, session_id)
            if not session_data:
                logger.warning(f"Could not load session {session_id} to persist context vars")
                return
            
            # Update or create context_vars field
            if "context_vars" not in session_data:
                session_data["context_vars"] = {}
            
            session_data["context_vars"].update(vars_to_update)
            
            # Save session
            await session_manager.save_session(session_data)
            logger.debug(f"Persisted context vars to session {session_id}: {list(vars_to_update.keys())}")
            
        except Exception as e:
            logger.warning(f"Failed to persist context vars to session {session_id}: {e}")
