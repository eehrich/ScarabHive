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
import json
import logging
import re

from agent_system.tools.schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class TaskSwitchServer(SchemaBasedToolServer):
    """Task state management server with optional precondition gates."""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)
        
        # Config for variable name and allowed tasks
        self._task_var_name = "current_task"
        self._allowed_tasks: Optional[List[str]] = None
        self._task_preconditions: Dict[str, Dict[str, Any]] = {}
        
        config_dict = server_config.config if hasattr(server_config, "config") else {}
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
        
        # Get template vars from agent config (lowest priority)
        template_vars = {}
        if agent and hasattr(agent, 'agent_config') and agent.agent_config:
            template_vars = dict(agent.agent_config.template_vars or {})
        
        # Get session-scoped template vars (highest priority - includes book_id from set_context)
        session_id = params.get("_session_id") if params else None
        if session_id and agent and hasattr(agent, '_session_tracker') and agent._session_tracker:
            session_template_vars = agent._session_tracker.get_session_template_vars(session_id)
            if session_template_vars:
                template_vars.update(session_template_vars)
                logger.debug(f"Added session template_vars to precondition check: {list(session_template_vars.keys())}")
        
        # Also include session metadata if available (for runtime vars)
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
            params: Either {"vars": <JSON string or dict>} (schema form) or
                   legacy flat key-value pairs. Keys starting with _ and
                   framework-injected runtime params are ignored.
        """
        status = params.get("_status")
        agent = params.get("_agent")
        session_id = params.get("_session_id")

        # Preferred form: a single `vars` argument carrying the key-value pairs.
        # Declared as a JSON string in the schema because freeform objects
        # (properties: {} + additionalProperties: true) get mangled by some
        # provider tool-call serializers — observed: OpenAI (gpt-5.x) strips
        # undeclared properties so calls arrive as {}, and Gemini's constrained
        # decoder used to collapse on freeform objects (same reason
        # comfyui_workflow.parameters is a JSON string). Dict is accepted too
        # for internal/test callers.
        # Framework-injected runtime params are never user context — they
        # used to pollute the session vars (a bare set_context() call
        # reported "Set: request_id=…" as success).
        _injected = {"request_id", "requestId", "vars"}

        def _clean(source: Dict[str, Any]) -> Dict[str, Any]:
            return {
                k: v for k, v in source.items()
                if not str(k).startswith('_') and k not in _injected
            }

        vars_arg = params.get("vars")
        if vars_arg is not None:
            if isinstance(vars_arg, str):
                try:
                    vars_arg = json.loads(vars_arg) if vars_arg.strip() else {}
                except json.JSONDecodeError as e:
                    if status:
                        await status.error(f"vars is not valid JSON: {e}")
                    return {"status": "error", "error": f"vars is not valid JSON: {e}"}
            if not isinstance(vars_arg, dict):
                if status:
                    await status.error("vars must be a JSON object or dict")
                return {"status": "error", "error": "vars must be a JSON object (e.g. '{\"book_id\": 42}')"}
            # Review-Befund: gleiche Hygiene wie im Legacy-Zweig (keine _-/
            # Framework-Keys als template_vars — sie erben sonst in alle
            # Sub-Agents), und flache non-internal Keys neben `vars` nicht
            # still verwerfen, sondern mitnehmen (`vars` gewinnt bei
            # Konflikt) — ein Mixed-Form-Call meldete sonst success,
            # obwohl Werte fehlten.
            context_vars = {**_clean(params), **_clean(vars_arg)}
        else:
            # Legacy flat form: every non-internal top-level key is a
            # context var.
            context_vars = _clean(params)

        if not context_vars:
            if status:
                await status.error("No context variables provided")
            return {
                "status": "error",
                "error": (
                    "No context variables provided. Pass them via the `vars` "
                    "argument as a JSON object string, e.g. "
                    "vars='{\"book_id\": 42}'."
                ),
            }
        
        # Get previous values from SESSION-SCOPED template vars (not agent_config!)
        # CRITICAL: This ensures session isolation - multiple sessions won't contaminate each other
        previous_values = {}

        # ORDERING INVARIANT — do NOT introduce an `await` before the tracker
        # write below.
        #
        # An LLM commonly emits set_context and a sub-agent spawn in the SAME
        # turn, and tool_execution runs those calls as concurrent asyncio tasks.
        # The spawn inherits the parent's live tracker vars (sub_agent_manager
        # create_sub_session), so the write here must land first. It does,
        # because this handler reaches the write with ZERO awaits while any
        # consumer yields to the loop at least once (the spawn does session I/O
        # first). Round-robin scheduling then guarantees the order regardless of
        # which tool call the LLM listed first.
        #
        # Add an await above this line and that guarantee silently disappears.
        # tests/plugins/test_plugin_task_switch_ordering.py pins it.
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
        Creates the session file if it doesn't exist yet (e.g., for new chats where
        set_context is called before the session is saved).
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
            
            # One write of the session at a time (SessionService.save_lock): a
            # rename or a save landing between this load and save was lost
            async with agent._session_service.save_lock(session_id):
                # Try to load current session, or create it if it doesn't exist
                session_data = None
                try:
                    session_data = await session_manager.load_session(user_id, session_id)
                except Exception as load_error:
                    # Session doesn't exist yet (new chat) - create it
                    logger.debug(f"Session {session_id} not found, creating it to persist context_vars: {load_error}")
                    try:
                        # Get agent name and llm_profile for session creation
                        agent_name = getattr(agent, 'name', 'unknown')
                        llm_profile = None
                        if hasattr(agent, 'agent_config') and agent.agent_config:
                            llm_profile = getattr(agent.agent_config, 'default_llm_profile', None)
                    
                        session_data = await session_manager.create_session(
                            user_id=user_id,
                            session_id=session_id,
                            title="New Session",
                            agent_name=agent_name,
                            llm_profile=llm_profile or "default"
                        )
                        logger.info(f"Created session {session_id} for user {user_id} to persist context_vars")
                    except Exception as create_error:
                        logger.warning(f"Could not create session {session_id} to persist context vars: {create_error}")
                        return
            
                if not session_data:
                    logger.warning(f"Could not load/create session {session_id} to persist context vars")
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
