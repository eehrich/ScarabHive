"""Shared test helper: collect ToolExecutionManager.execute_tools_streaming output.

Replaces the removed production convenience wrapper
``ToolExecutionManager.execute_tools`` (test-only production code). Production only ever consumes the
streaming generator; tests that just want the final tuple use this collector.

Importable from any test dir via ``pythonpath = src tests`` (pytest.ini).
"""
from typing import Any, Dict, List, Optional, Tuple

from agent_system.llm.models import ChatMessage


async def execute_tools_collect(
    manager: Any,
    tool_calls: List[Dict],
    tool_name_mapping: Dict[str, str],
    available_tools: List[str],
    step: int,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Tuple[List[ChatMessage], List[Dict], List[Dict]]:
    """Run execute_tools_streaming to completion and return
    (tool_messages, events, results) — the old execute_tools contract."""
    tool_messages: List[ChatMessage] = []
    events: List[Dict] = []
    results: List[Dict] = []
    async for item in manager.execute_tools_streaming(
            tool_calls, tool_name_mapping, available_tools, step, request_id,
            session_id=session_id, user_id=user_id):
        if item["type"] == "tool_events":
            events.extend(item["events"])
        elif item["type"] == "complete":
            tool_messages = item["messages"]
            results = item["results"]
    return tool_messages, events, results
