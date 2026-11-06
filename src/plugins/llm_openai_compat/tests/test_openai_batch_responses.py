"""The batch backend on /v1/responses (``batch_provider: openai_responses``).

OpenAI's reasoning models refuse function calls on Chat Completions; their
batch twins sent every tool request there. The lines are built and the
results read by the Responses client itself, so both are driven here through
the batch client's own file writer and result parser -- only the upload and
the download are swapped.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_system.config.models import LLMModelConfig
from agent_system.llm.batch.models import BatchJob, BatchRequest
from plugins.llm_openai_compat.openai_batch import CHAT_COMPLETIONS, RESPONSES, OpenAIBatchClient
from plugins.llm_openai_compat.provider import BATCH_BACKENDS

TOOL = {"type": "function", "function": {"name": "f", "description": "d",
                                         "parameters": {"type": "object", "properties": {}}}}


def _job():
    return BatchJob(requests=[BatchRequest(custom_id="r1", model="gpt-x", max_tokens=100,
                                           messages=[{"role": "user", "content": "hi"}], tools=[TOOL])])


def test_the_responses_backend_is_registered_under_its_name():
    backend = BATCH_BACKENDS["openai_responses"](LLMModelConfig(provider="batch", model="gpt-x", api_key="k"))
    assert backend.endpoint == RESPONSES
    assert BATCH_BACKENDS["openai_httpx"](LLMModelConfig(provider="batch", model="gpt-x", api_key="k")
                                          ).endpoint == CHAT_COMPLETIONS


def test_a_line_goes_to_the_responses_api_in_its_shape(tmp_path):
    client = OpenAIBatchClient(api_key="k", endpoint=RESPONSES)
    path = tmp_path / "in.jsonl"
    client._create_input_file(_job(), path)
    line = json.loads(path.read_text(encoding="utf-8"))
    assert line["url"] == RESPONSES
    body = line["body"]
    assert body["input"][0]["role"] == "user" and "messages" not in body
    assert body["tools"][0]["name"] == "f", "the tool did not take the Responses shape"
    assert body["max_output_tokens"] == 100
    assert body["include"] == ["reasoning.encrypted_content"]


async def test_the_batch_is_created_for_the_endpoint_its_lines_name():
    client = OpenAIBatchClient(api_key="k", endpoint=RESPONSES)
    client._client = AsyncMock()
    client._client.post = AsyncMock(return_value=AsyncMock(status_code=200, json=lambda: {"id": "b"}))
    with patch.object(client, "_raise_for_status"):
        await client._create_batch("file_1")
    assert client._client.post.call_args.kwargs["json"]["endpoint"] == RESPONSES


async def test_a_result_is_read_as_the_responses_client_reads_it():
    client = OpenAIBatchClient(api_key="k", endpoint=RESPONSES)
    job = _job()
    job.provider_job_id = "b1"
    body = {"model": "gpt-x-2026-01-01", "status": "completed", "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC"},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "f", "arguments": "{}"}],
        "usage": {"input_tokens": 10, "output_tokens": 5}}
    line = json.dumps({"custom_id": "r1", "response": {"status_code": 200, "body": body}, "error": None})
    with patch.object(client, "get_batch_status", AsyncMock(return_value={"output_file_id": "o1"})), \
            patch.object(client, "_download_file", AsyncMock(return_value=line)):
        (result,) = await client.get_batch_results(job)
    assistant = result["response"]["assistant"]
    assert assistant["tool_calls"][0]["function"]["name"] == "f"
    (block,) = assistant["reasoning_details"]
    assert block["model"] == "gpt-x", "tagged with the snapshot, the next sync request takes it as foreign"
    assert block["items"][0]["encrypted_content"] == "ENC"
    assert result["usage"]["prompt_tokens"] == 10


def test_a_line_takes_the_model_entrys_settings(tmp_path):
    """Built without them, a line lost the entry's reasoning level and its images."""
    cfg = LLMModelConfig(provider="batch", model="gpt-x", api_key="k", thinking_level="high",
                         capabilities={"image_input": True})
    client = BATCH_BACKENDS["openai_responses"](cfg)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    job = BatchJob(requests=[BatchRequest(custom_id="r1", model="gpt-x",
                                          messages=[{"role": "user", "content": [{"type": "text", "text": "look"},
                                                                                  image]}])])
    path = tmp_path / "in.jsonl"
    client._create_input_file(job, path)
    body = json.loads(path.read_text(encoding="utf-8"))["body"]
    assert body["reasoning"]["effort"] == "high"
    assert any(part.get("type") == "input_image" for part in body["input"][0]["content"]), body["input"]


def test_a_broken_chain_is_not_replayed_in_a_batch(tmp_path):
    """The queue keeps rd_orphaned for the converter: a verbatim replay of an orphaned turn is a 400."""
    client = OpenAIBatchClient(api_key="k", endpoint=RESPONSES)
    turn = client._responses_client("gpt-x")._format_response({"output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]})["assistant"]
    for orphaned, replayed in ((False, True), (True, False)):
        job = BatchJob(requests=[BatchRequest(custom_id="r1", model="gpt-x", messages=[
            {"role": "user", "content": "go"}, {**turn, "rd_orphaned": orphaned}, {"role": "user", "content": "on"}])])
        path = tmp_path / "in.jsonl"
        client._create_input_file(job, path)
        items = json.loads(path.read_text(encoding="utf-8"))["body"]["input"]
        assert any(i.get("type") == "reasoning" for i in items) is replayed


def test_a_chat_line_leaves_another_models_call_signatures_home(tmp_path):
    """The queue keeps reasoning_model; the sync routes already filtered by it."""
    client = OpenAIBatchClient(api_key="k")
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "U0lH"}}}
    for producer, kept in (("gpt-x", True), ("gemini-x", False)):
        job = BatchJob(requests=[BatchRequest(custom_id="r1", model="gpt-x", messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [call], "reasoning_model": producer},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}])])
        path = tmp_path / "in.jsonl"
        client._create_input_file(job, path)
        sent = json.loads(path.read_text(encoding="utf-8"))["body"]["messages"][1]
        assert ("extra_content" in sent["tool_calls"][0]) is kept, sent
        assert "reasoning_model" not in sent
