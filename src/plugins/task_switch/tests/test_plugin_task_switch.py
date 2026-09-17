"""Tests for task_switch plugin."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.task_switch.server import TaskSwitchServer
from agent_system.config.models import ToolServerConfig, AgentConfig


@pytest.fixture
def mock_system_config():
    config = MagicMock()
    config.ssl_verify = True
    return config


@pytest.fixture
def mock_server_config():
    return ToolServerConfig(type="task_switch", enabled=True)


@pytest.fixture
def mock_server_config_with_allowed_tasks():
    return ToolServerConfig(
        type="task_switch", 
        enabled=True, 
        config={"allowed_tasks": ["init", "analyze", "execute", "review"]}
    )


@pytest.fixture
def mock_server_config_with_preconditions():
    """Config with task preconditions (gate checks)."""
    return ToolServerConfig(
        type="task_switch",
        enabled=True,
        config={
            "task_var_name": "workflow_phase",
            "allowed_tasks": ["planning", "structure", "content", "review"],
            "task_preconditions": {
                "structure": {
                    "tool": "validation_tool",
                    "params": {"book_id": "{{ book_id }}", "scope": "story"},
                    "gate_field": "summary.can_proceed",
                    "error_field": "summary.gate_reason"
                },
                "content": {
                    "tool": "validation_tool",
                    "params": {"scope": "structure"},
                    "gate_field": "can_proceed"
                }
            }
        }
    )


@pytest.fixture
def server(mock_system_config, mock_server_config):
    return TaskSwitchServer("task_switch", mock_system_config, mock_server_config)


@pytest.fixture
def server_with_restrictions(mock_system_config, mock_server_config_with_allowed_tasks):
    return TaskSwitchServer("task_switch", mock_system_config, mock_server_config_with_allowed_tasks)


@pytest.fixture
def server_with_preconditions(mock_system_config, mock_server_config_with_preconditions):
    return TaskSwitchServer("task_switch", mock_system_config, mock_server_config_with_preconditions)


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.agent_config = AgentConfig(template_vars={"current_task": "init"})
    return agent


@pytest.fixture
def mock_agent_with_book_id():
    """Agent with book_id in template_vars for precondition testing."""
    agent = MagicMock()
    agent.agent_config = AgentConfig(template_vars={
        "workflow_phase": "planning",
        "book_id": "42"
    })
    return agent


class TestTaskSwitchServer:
    def test_init_default_var(self, server):
        assert server._task_var_name == "current_task"
        assert server._allowed_tasks is None

    def test_init_custom_var(self, mock_system_config):
        config = ToolServerConfig(type="task_switch", enabled=True, config={"task_var_name": "state"})
        server = TaskSwitchServer("task_switch", mock_system_config, config)
        assert server._task_var_name == "state"

    def test_init_with_allowed_tasks(self, server_with_restrictions):
        assert server_with_restrictions._allowed_tasks == ["init", "analyze", "execute", "review"]

    def test_get_template_vars(self, server_with_restrictions):
        vars = server_with_restrictions.get_template_vars()
        assert "allowed_tasks" in vars
        assert vars["allowed_tasks"] == ["init", "analyze", "execute", "review"]

    def test_get_template_vars_empty_when_no_restrictions(self, server):
        vars = server.get_template_vars()
        assert vars["allowed_tasks"] == []


class TestSetTask:
    @pytest.mark.asyncio
    async def test_set_task_basic(self, server, mock_agent):
        result = await server.set_task({"task_name": "analyze", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["previous_task"] == "init"
        assert result["current_task"] == "analyze"
        assert mock_agent.agent_config.template_vars["current_task"] == "analyze"

    @pytest.mark.asyncio
    async def test_set_task_empty_error(self, server, mock_agent):
        result = await server.set_task({"task_name": "", "_agent": mock_agent})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_set_task_no_agent(self, server):
        result = await server.set_task({"task_name": "test", "_agent": None})
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_set_task_initializes_template_vars(self, server):
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars=None)
        result = await server.set_task({"task_name": "plan", "_agent": agent})
        assert result["status"] == "success"
        assert agent.agent_config.template_vars["current_task"] == "plan"

    @pytest.mark.asyncio
    async def test_set_task_with_status(self, server, mock_agent):
        mock_status = AsyncMock()
        result = await server.set_task({"task_name": "review", "_agent": mock_agent, "_status": mock_status})
        assert result["status"] == "success"
        mock_status.end.assert_called_once()
        # Status should show transition
        call_args = mock_status.end.call_args[0][0]
        assert "review" in call_args

    @pytest.mark.asyncio
    async def test_multiple_transitions(self, server, mock_agent):
        await server.set_task({"task_name": "analyze", "_agent": mock_agent})
        result = await server.set_task({"task_name": "execute", "_agent": mock_agent})
        assert result["previous_task"] == "analyze"
        assert result["current_task"] == "execute"


class TestAllowedTasksValidation:
    @pytest.mark.asyncio
    async def test_allowed_task_succeeds(self, server_with_restrictions, mock_agent):
        """Valid task from allowed list should succeed."""
        result = await server_with_restrictions.set_task({"task_name": "analyze", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["current_task"] == "analyze"

    @pytest.mark.asyncio
    async def test_invalid_task_returns_error(self, server_with_restrictions, mock_agent):
        """Invalid task not in allowed list should return error."""
        result = await server_with_restrictions.set_task({"task_name": "invalid_task", "_agent": mock_agent})
        assert result["status"] == "error"
        assert "Invalid task" in result["error"]
        assert "invalid_task" in result["error"]
        assert "Allowed tasks" in result["error"]

    @pytest.mark.asyncio
    async def test_no_restrictions_allows_any_task(self, server, mock_agent):
        """Without allowed_tasks config, any task should work."""
        result = await server.set_task({"task_name": "arbitrary_task", "_agent": mock_agent})
        assert result["status"] == "success"
        assert result["current_task"] == "arbitrary_task"

    @pytest.mark.asyncio
    async def test_all_allowed_tasks_work(self, server_with_restrictions, mock_agent):
        """All tasks in the allowed list should work."""
        for task in ["init", "analyze", "execute", "review"]:
            result = await server_with_restrictions.set_task({"task_name": task, "_agent": mock_agent})
            assert result["status"] == "success", f"Task '{task}' should be allowed"
            assert result["current_task"] == task


class TestPreconditions:
    """Tests for task precondition (gate check) functionality."""

    def test_init_loads_preconditions(self, server_with_preconditions):
        """Server should load task_preconditions from config."""
        assert server_with_preconditions._task_preconditions is not None
        assert "structure" in server_with_preconditions._task_preconditions
        assert "content" in server_with_preconditions._task_preconditions
        assert server_with_preconditions._task_preconditions["structure"]["tool"] == "validation_tool"

    def test_get_template_vars_shows_preconditions(self, server_with_preconditions):
        """Template vars should indicate if preconditions are configured."""
        vars = server_with_preconditions.get_template_vars()
        assert vars["has_preconditions"] is True

    def test_get_template_vars_no_preconditions(self, server):
        """Template vars should show no preconditions when not configured."""
        vars = server.get_template_vars()
        assert vars["has_preconditions"] is False

    @pytest.mark.asyncio
    async def test_task_without_precondition_succeeds(self, server_with_preconditions, mock_agent_with_book_id):
        """Task without precondition should succeed directly."""
        # "planning" has no precondition
        result = await server_with_preconditions.set_task({
            "task_name": "planning",
            "_agent": mock_agent_with_book_id
        })
        assert result["status"] == "success"
        assert result["current_task"] == "planning"

    @pytest.mark.asyncio
    async def test_precondition_gate_open(self, server_with_preconditions, mock_agent_with_book_id):
        """Task with passing precondition should succeed."""
        # Mock the call_tool to return gate_open result
        mock_agent_with_book_id.call_tool = AsyncMock(return_value={
            "status": "success",
            "summary": {
                "can_proceed": True,
                "status": "approved"
            }
        })
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": mock_agent_with_book_id
        })
        
        assert result["status"] == "success"
        assert result["current_task"] == "structure"
        # Verify tool was called with rendered params
        mock_agent_with_book_id.call_tool.assert_called_once()
        call_args = mock_agent_with_book_id.call_tool.call_args
        assert call_args[0][0] == "validation_tool"
        assert call_args[0][1]["book_id"] == "42"  # Template var rendered
        assert call_args[0][1]["scope"] == "story"

    @pytest.mark.asyncio
    async def test_precondition_gate_closed(self, server_with_preconditions, mock_agent_with_book_id):
        """Task with failing precondition should be blocked."""
        mock_agent_with_book_id.call_tool = AsyncMock(return_value={
            "status": "success",
            "summary": {
                "can_proceed": False,
                "gate_reason": "Story needs 2 more approvals"
            }
        })
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": mock_agent_with_book_id
        })
        
        assert result["status"] == "blocked"
        assert result["gate_closed"] is True
        assert result["reason"] == "Story needs 2 more approvals"
        assert result["gate_field"] == "summary.can_proceed"
        assert result["gate_value"] is False
        # Template vars should NOT be changed
        assert mock_agent_with_book_id.agent_config.template_vars["workflow_phase"] == "planning"

    @pytest.mark.asyncio
    async def test_precondition_gate_closed_with_status(self, server_with_preconditions, mock_agent_with_book_id):
        """Blocked task should report error via status callback."""
        mock_agent_with_book_id.call_tool = AsyncMock(return_value={
            "summary": {"can_proceed": False, "gate_reason": "Not ready"}
        })
        mock_status = AsyncMock()
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": mock_agent_with_book_id,
            "_status": mock_status
        })
        
        assert result["status"] == "blocked"
        mock_status.error.assert_called_once()
        assert "Gate closed" in mock_status.error.call_args[0][0]

    @pytest.mark.asyncio
    async def test_precondition_tool_error(self, server_with_preconditions, mock_agent_with_book_id):
        """Precondition tool error should block the task."""
        mock_agent_with_book_id.call_tool = AsyncMock(side_effect=Exception("Tool failed"))
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": mock_agent_with_book_id
        })
        
        assert result["status"] == "blocked"
        assert "Tool failed" in result["reason"]

    @pytest.mark.asyncio
    async def test_precondition_result_included(self, server_with_preconditions, mock_agent_with_book_id):
        """Blocked result should include the precondition tool's result."""
        tool_result = {
            "status": "success",
            "summary": {
                "can_proceed": False,
                "gate_reason": "Need more reviews",
                "valid_approvals": 2,
                "required_approvals": 4
            }
        }
        mock_agent_with_book_id.call_tool = AsyncMock(return_value=tool_result)
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": mock_agent_with_book_id
        })
        
        assert result["status"] == "blocked"
        assert result["precondition_result"] == tool_result
        # LLM can see detailed info from precondition result
        assert result["precondition_result"]["summary"]["valid_approvals"] == 2

    @pytest.mark.asyncio
    async def test_precondition_without_agent_skips_check(self, server_with_preconditions):
        """Without agent, precondition check is skipped."""
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": None
        })
        # No agent = can't call tool, so precondition is skipped
        assert result["status"] == "success"


class TestTemplateRendering:
    """Tests for Jinja2-style template variable rendering in precondition params."""

    def test_render_simple_string(self, server_with_preconditions):
        """Simple {{ var }} should be replaced."""
        result = server_with_preconditions._render_template_value(
            "{{ book_id }}",
            {"book_id": "123"}
        )
        assert result == "123"

    def test_render_string_with_spaces(self, server_with_preconditions):
        """{{ var }} with spaces should work."""
        result = server_with_preconditions._render_template_value(
            "{{  book_id  }}",
            {"book_id": "456"}
        )
        assert result == "456"

    def test_render_missing_var_unchanged(self, server_with_preconditions):
        """Missing variable should keep the template unchanged."""
        result = server_with_preconditions._render_template_value(
            "{{ missing_var }}",
            {"book_id": "123"}
        )
        assert result == "{{ missing_var }}"

    def test_render_dict(self, server_with_preconditions):
        """Dict values should be recursively rendered."""
        result = server_with_preconditions._render_template_value(
            {"book_id": "{{ id }}", "scope": "story"},
            {"id": "42"}
        )
        assert result == {"book_id": "42", "scope": "story"}

    def test_render_list(self, server_with_preconditions):
        """List values should be recursively rendered."""
        result = server_with_preconditions._render_template_value(
            ["{{ a }}", "{{ b }}"],
            {"a": "1", "b": "2"}
        )
        assert result == ["1", "2"]

    def test_render_nested_dict(self, server_with_preconditions):
        """Nested structures should be fully rendered."""
        result = server_with_preconditions._render_template_value(
            {"outer": {"inner": "{{ val }}"}},
            {"val": "deep"}
        )
        assert result == {"outer": {"inner": "deep"}}


class TestNestedFieldAccess:
    """Tests for dot-notation field access in gate results."""

    def test_get_nested_simple(self, server):
        """Simple field access."""
        data = {"can_proceed": True}
        assert server._get_nested_value(data, "can_proceed") is True

    def test_get_nested_one_level(self, server):
        """One level nested access."""
        data = {"summary": {"can_proceed": True}}
        assert server._get_nested_value(data, "summary.can_proceed") is True

    def test_get_nested_two_levels(self, server):
        """Two levels nested access."""
        data = {"result": {"summary": {"status": "approved"}}}
        assert server._get_nested_value(data, "result.summary.status") == "approved"

    def test_get_nested_missing_returns_none(self, server):
        """Missing path should return None."""
        data = {"summary": {"other": "value"}}
        assert server._get_nested_value(data, "summary.can_proceed") is None

    def test_get_nested_partial_path_missing(self, server):
        """Partial path missing should return None."""
        data = {"other": {}}
        assert server._get_nested_value(data, "summary.can_proceed") is None


class TestSetContext:
    """Tests for set_context tool - runtime template_vars management."""

    @pytest.mark.asyncio
    async def test_set_context_basic(self, server, mock_agent):
        """Basic context setting should work."""
        result = await server.set_context({
            "book_id": "42",
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": "42"}
        assert mock_agent.agent_config.template_vars["book_id"] == "42"

    @pytest.mark.asyncio
    async def test_set_context_multiple_vars(self, server, mock_agent):
        """Setting multiple context vars at once should work."""
        result = await server.set_context({
            "book_id": "123",
            "chapter_id": "456",
            "mode": "edit",
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": "123", "chapter_id": "456", "mode": "edit"}
        assert mock_agent.agent_config.template_vars["book_id"] == "123"
        assert mock_agent.agent_config.template_vars["chapter_id"] == "456"
        assert mock_agent.agent_config.template_vars["mode"] == "edit"

    @pytest.mark.asyncio
    async def test_set_context_returns_previous_values(self, server, mock_agent):
        """Should return previous values for changed vars."""
        mock_agent.agent_config.template_vars["book_id"] = "old_value"
        result = await server.set_context({
            "book_id": "new_value",
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["previous"]["book_id"] == "old_value"
        assert result["updated"]["book_id"] == "new_value"

    @pytest.mark.asyncio
    async def test_set_context_ignores_internal_params(self, server, mock_agent):
        """Internal params starting with _ should be ignored."""
        result = await server.set_context({
            "book_id": "42",
            "_agent": mock_agent,
            "_status": AsyncMock(),
            "_session_id": "test123"
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": "42"}
        assert "_agent" not in mock_agent.agent_config.template_vars
        assert "_status" not in mock_agent.agent_config.template_vars

    @pytest.mark.asyncio
    async def test_set_context_no_vars_error(self, server, mock_agent):
        """Should error when no context vars provided."""
        result = await server.set_context({
            "_agent": mock_agent
        })
        assert result["status"] == "error"
        assert "No context variables" in result["error"]

    @pytest.mark.asyncio
    async def test_set_context_vars_json_string(self, server, mock_agent):
        """Schema form: vars as JSON object string (robust against provider
        serializers that strip undeclared properties)."""
        result = await server.set_context({
            "vars": '{"book_id": 42, "phase": "review"}',
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": 42, "phase": "review"}
        assert mock_agent.agent_config.template_vars["book_id"] == 42
        assert mock_agent.agent_config.template_vars["phase"] == "review"

    @pytest.mark.asyncio
    async def test_set_context_vars_dict(self, server, mock_agent):
        """vars accepts a dict too (internal/test callers)."""
        result = await server.set_context({
            "vars": {"book_id": 7},
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": 7}

    @pytest.mark.asyncio
    async def test_set_context_vars_invalid_json(self, server, mock_agent):
        """Malformed JSON in vars must produce a clear error."""
        result = await server.set_context({
            "vars": '{book_id: 42',
            "_agent": mock_agent
        })
        assert result["status"] == "error"
        assert "not valid JSON" in result["error"]

    @pytest.mark.asyncio
    async def test_set_context_vars_non_object(self, server, mock_agent):
        """vars JSON that is not an object must error."""
        result = await server.set_context({
            "vars": '[1, 2, 3]',
            "_agent": mock_agent
        })
        assert result["status"] == "error"
        assert "JSON object" in result["error"]

    @pytest.mark.asyncio
    async def test_set_context_ignores_injected_runtime_params(self, server, mock_agent):
        """Framework-injected request_id/requestId must not become context vars
        (a bare call with only injected params used to report success)."""
        result = await server.set_context({
            "request_id": "abc_003",
            "requestId": "abc_003",
            "_agent": mock_agent
        })
        assert result["status"] == "error"
        assert "No context variables" in result["error"]

    @pytest.mark.asyncio
    async def test_set_context_vars_payload_is_sanitized(self, server, mock_agent):
        """Review-Befund: der vars-Zweig braucht dieselbe Hygiene wie der
        Legacy-Zweig — _-Keys/Framework-Params im vars-Payload dürfen nicht
        als template_vars landen (sie erben sonst in alle Sub-Agents)."""
        result = await server.set_context({
            "vars": '{"_agent": "x", "request_id": "r", "book_id": 42}',
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": 42}
        assert "_agent" not in mock_agent.agent_config.template_vars
        assert "request_id" not in mock_agent.agent_config.template_vars

    @pytest.mark.asyncio
    async def test_set_context_mixed_form_merges_flat_keys(self, server, mock_agent):
        """Review-Befund: flache Keys neben vars dürfen nicht still verworfen
        werden (Mixed-Form-Call meldete success, obwohl Werte fehlten) —
        vars gewinnt bei Konflikt."""
        result = await server.set_context({
            "vars": '{"phase": "review"}',
            "book_id": 42,
            "phase": "IGNORED-flat-loses-conflict",
            "_agent": mock_agent
        })
        assert result["status"] == "success"
        assert result["updated"] == {"book_id": 42, "phase": "review"}

    @pytest.mark.asyncio
    async def test_set_context_no_agent_error(self, server):
        """Should error when no agent provided."""
        result = await server.set_context({
            "book_id": "42",
            "_agent": None
        })
        assert result["status"] == "error"
        assert "agent_config" in result["error"] or "session_tracker" in result["error"]

    @pytest.mark.asyncio
    async def test_set_context_initializes_template_vars(self, server):
        """Should initialize template_vars if None."""
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars=None)
        result = await server.set_context({
            "book_id": "42",
            "_agent": agent
        })
        assert result["status"] == "success"
        assert agent.agent_config.template_vars["book_id"] == "42"

    @pytest.mark.asyncio
    async def test_set_context_with_status(self, server, mock_agent):
        """Should call status.end when status provided."""
        mock_status = AsyncMock()
        result = await server.set_context({
            "book_id": "42",
            "_agent": mock_agent,
            "_status": mock_status
        })
        assert result["status"] == "success"
        mock_status.end.assert_called_once()
        # Should contain info about what was set
        call_args = mock_status.end.call_args[0][0]
        assert "book_id" in call_args
        assert "42" in call_args
    
    @pytest.mark.asyncio
    async def test_set_context_error_calls_status_error(self, server):
        """Should call status.error when no agent config available."""
        mock_status = AsyncMock()
        mock_agent = MagicMock()
        mock_agent.agent_config = None
        # Also mock _session_tracker to None so fallback fails too
        mock_agent._session_tracker = None
        
        result = await server.set_context({
            "book_id": "42",
            "_agent": mock_agent,
            "_status": mock_status
        })
        assert result["status"] == "error"
        mock_status.error.assert_called_once()
        call_args = mock_status.error.call_args[0][0]
        assert "agent_config" in call_args or "session_tracker" in call_args
    
    @pytest.mark.asyncio
    async def test_set_context_no_vars_calls_status_error(self, server, mock_agent):
        """Should call status.error when no context variables provided."""
        mock_status = AsyncMock()
        
        result = await server.set_context({
            "_agent": mock_agent,
            "_status": mock_status
            # No actual vars, just internal params
        })
        assert result["status"] == "error"
        mock_status.error.assert_called_once()
        call_args = mock_status.error.call_args[0][0]
        assert "No context variables" in call_args


class TestPreconditionWithSetContext:
    """Integration tests: set_context followed by precondition checks."""

    @pytest.mark.asyncio
    async def test_precondition_uses_context_from_set_context(self, server_with_preconditions):
        """Precondition should use book_id set via set_context."""
        # Create agent with empty book_id
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars={
            "workflow_phase": "planning",
            "book_id": ""  # Empty initially
        })
        
        # Mock call_tool to capture what params were passed
        captured_params = {}
        async def mock_call_tool(tool_name, params):
            captured_params.update(params)
            return {"summary": {"can_proceed": True}}
        agent.call_tool = mock_call_tool
        
        # First set the context
        await server_with_preconditions.set_context({
            "book_id": "17",
            "_agent": agent
        })
        
        # Now try to switch task - precondition should use book_id=17
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": agent
        })
        
        assert result["status"] == "success"
        assert captured_params.get("book_id") == "17"

    @pytest.mark.asyncio
    async def test_precondition_fails_with_empty_book_id(self, server_with_preconditions):
        """Precondition should fail gracefully when book_id is empty."""
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars={
            "workflow_phase": "planning",
            "book_id": ""  # Empty - will cause validation to fail
        })
        
        async def mock_call_tool(tool_name, params):
            # Simulate what production_status returns for empty book_id
            return {"status": "error", "error": "book_id is required"}
        agent.call_tool = mock_call_tool
        
        result = await server_with_preconditions.set_task({
            "task_name": "structure",
            "_agent": agent
        })
        
        # Should be blocked because gate_field (summary.can_proceed) is not True
        assert result["status"] == "blocked"
        assert result["gate_closed"] is True


class TestContextPersistence:
    """Tests for context_vars persistence to session storage."""

    @pytest.mark.asyncio
    async def test_set_context_persists_to_session(self, server):
        """set_context should persist vars to session storage."""
        # Create mock agent with session service
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars={})
        
        # Mock session manager
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "session_id": "test_session",
            "user_id": "test_user",
            "messages": []
        })
        mock_session_manager.save_session = AsyncMock()
        
        agent._session_service = MagicMock()
        agent._session_service.session_manager = mock_session_manager
        
        # Mock session tracker for user_id
        agent._session_tracker = MagicMock()
        agent._session_tracker.get_session_metadata = MagicMock(return_value={"user_id": "test_user"})
        
        # Call set_context with session_id
        result = await server.set_context({
            "book_id": "42",
            "_agent": agent,
            "_session_id": "test_session"
        })
        
        assert result["status"] == "success"
        assert result["persisted"] is True
        
        # Verify save_session was called with context_vars
        mock_session_manager.save_session.assert_called_once()
        saved_data = mock_session_manager.save_session.call_args[0][0]
        assert "context_vars" in saved_data
        assert saved_data["context_vars"]["book_id"] == "42"

    @pytest.mark.asyncio
    async def test_set_task_persists_to_session(self, server):
        """set_task should persist task var to session storage."""
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars={"current_task": "init"})
        
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "session_id": "test_session",
            "user_id": "test_user",
            "messages": []
        })
        mock_session_manager.save_session = AsyncMock()
        
        agent._session_service = MagicMock()
        agent._session_service.session_manager = mock_session_manager
        agent._session_tracker = MagicMock()
        agent._session_tracker.get_session_metadata = MagicMock(return_value={"user_id": "test_user"})
        
        result = await server.set_task({
            "task_name": "analyze",
            "_agent": agent,
            "_session_id": "test_session"
        })
        
        assert result["status"] == "success"
        
        # Verify context_vars includes the task var
        saved_data = mock_session_manager.save_session.call_args[0][0]
        assert saved_data["context_vars"]["current_task"] == "analyze"

    @pytest.mark.asyncio
    async def test_set_context_without_session_id_no_persist(self, server, mock_agent):
        """Without session_id, context should not be persisted."""
        # Add mock session service that should NOT be called
        mock_session_manager = AsyncMock()
        mock_agent._session_service = MagicMock()
        mock_agent._session_service.session_manager = mock_session_manager
        
        result = await server.set_context({
            "book_id": "42",
            "_agent": mock_agent
            # No _session_id
        })
        
        assert result["status"] == "success"
        assert result["persisted"] is False
        # save_session should NOT have been called
        mock_session_manager.save_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_merges_with_existing_context_vars(self, server):
        """Persisting should merge with existing context_vars."""
        agent = MagicMock()
        agent.agent_config = AgentConfig(template_vars={})
        
        # Session already has some context_vars
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(return_value={
            "session_id": "test_session",
            "user_id": "test_user",
            "messages": [],
            "context_vars": {"existing_key": "existing_value"}
        })
        mock_session_manager.save_session = AsyncMock()
        
        agent._session_service = MagicMock()
        agent._session_service.session_manager = mock_session_manager
        agent._session_tracker = MagicMock()
        agent._session_tracker.get_session_metadata = MagicMock(return_value={"user_id": "test_user"})
        
        result = await server.set_context({
            "book_id": "42",
            "_agent": agent,
            "_session_id": "test_session"
        })
        
        assert result["status"] == "success"
        
        # Both old and new keys should be present
        saved_data = mock_session_manager.save_session.call_args[0][0]
        assert saved_data["context_vars"]["existing_key"] == "existing_value"
        assert saved_data["context_vars"]["book_id"] == "42"

    @pytest.mark.asyncio
    async def test_persist_creates_session_if_not_exists(self, server):
        """Persisting should create session if it doesn't exist yet (new chat)."""
        from agent_system.services.session_manager import SessionNotFoundError
        
        agent = MagicMock()
        agent.name = "test_agent"
        agent.agent_config = AgentConfig(template_vars={}, llm_profile="chat")
        
        # Simulate session not found, then creation
        mock_session_manager = AsyncMock()
        mock_session_manager.load_session = AsyncMock(side_effect=SessionNotFoundError("Session not found"))
        mock_session_manager.create_session = AsyncMock(return_value={
            "session_id": "new_session",
            "user_id": "test_user",
            "messages": []
        })
        mock_session_manager.save_session = AsyncMock()
        
        agent._session_service = MagicMock()
        agent._session_service.session_manager = mock_session_manager
        agent._session_tracker = MagicMock()
        agent._session_tracker.get_session_metadata = MagicMock(return_value={"user_id": "test_user"})
        agent._session_tracker.get_session_template_vars = MagicMock(return_value={})
        agent._session_tracker.set_session_template_vars = MagicMock()
        
        result = await server.set_context({
            "book_id": "42",
            "_agent": agent,
            "_session_id": "new_session"
        })
        
        assert result["status"] == "success"
        assert result["persisted"] is True
        
        # Verify create_session was called
        mock_session_manager.create_session.assert_called_once()
        create_call = mock_session_manager.create_session.call_args
        assert create_call[1]["session_id"] == "new_session"
        
        # Verify save_session was called with context_vars
        mock_session_manager.save_session.assert_called_once()
        saved_data = mock_session_manager.save_session.call_args[0][0]
        assert saved_data["context_vars"]["book_id"] == "42"
