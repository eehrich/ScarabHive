"""_multimodal_content items a tool returns are converted one by one."""
import json
from unittest.mock import AsyncMock, MagicMock

from agent_system.servers.agent.components.tool_execution import ToolExecutionManager
from tool_execution_test_helpers import execute_tools_collect


async def test_an_invalid_item_is_dropped_and_the_result_survives(tmp_path):
    """One item without a path must not turn the whole result -- a job that already
    ran -- into an error; the valid items stay attached."""
    image = tmp_path / "out.png"
    image.write_bytes(b"png")
    server = MagicMock()
    server.call_with_status = AsyncMock(return_value={
        "status": "completed",
        "outputs": {"text": [{"content": "caption"}]},
        "_multimodal_content": [
            {"type": "text", "mime_type": "text/plain", "content": "caption"},  # no path
            {"type": "image", "path": str(image), "mime_type": "image/png"},
        ],
    })
    registry = MagicMock()
    registry.list.return_value = ["comfyui"]
    registry.get.return_value = server
    tool_calls = [{"id": "call_1", "function": {"name": "comfyui", "arguments": "{}"}}]

    messages, _, _ = await execute_tools_collect(
        ToolExecutionManager(registry), tool_calls, {"comfyui": "comfyui"}, ["comfyui"], 1,
        request_id="req_1",
    )

    assert len(messages) == 1
    content = json.loads(messages[0].content)
    assert content["status"] == "completed"
    assert "_multimodal_content" not in content
    assert [item.path for item in messages[0].multimodal_content] == [str(image)]
