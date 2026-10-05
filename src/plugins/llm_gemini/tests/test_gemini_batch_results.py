"""A batch result carries the same answer and usage as a chat call.

Thinking tokens count as output (Gemini reports them apart from the answer and
bills them as output), and a thinking part is no part of the answer text.
"""
from unittest.mock import MagicMock, patch

from google.genai import types

from agent_system.llm.batch.models import BatchJob, BatchRequest
from plugins.llm_gemini.gemini_batch import GeminiBatchClient


async def test_a_batch_answer_keeps_thinking_apart_and_counts_it_as_output():
    with patch("google.genai.Client"):
        client = GeminiBatchClient(api_key="test-key")
    answer = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[
            types.Part(text="hm", thought=True), types.Part(text="da")]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=7, candidates_token_count=3, thoughts_token_count=40,
            total_token_count=50))
    client._sdk_client.batches.get = MagicMock(return_value=types.BatchJob(
        dest=types.BatchJobDestination(inlined_responses=[types.InlinedResponse(response=answer)])))
    job = BatchJob(provider_job_id="batches/x",
                   requests=[BatchRequest(custom_id="r1", messages=[], model="m")])

    [result] = await client.get_batch_results(job)

    message = result["response"]["choices"][0]["message"]
    assert (message["content"], message.get("reasoning_content")) == ("da", "hm")
    usage = result["response"]["usage"]
    assert usage["completion_tokens"] == 43
    assert usage["completion_tokens_details"] == {"reasoning_tokens": 40}
