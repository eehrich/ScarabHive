"""quota_exhausted on the bodies providers really send with a 429.

A throttle read as a spent quota blocks the model for every agent for max_block_seconds; the old test
("quota" or "exhausted" anywhere) did that to Google's ordinary RESOURCE_EXHAUSTED.
"""
import pytest

from plugins.llm_common.quota import quota_exhausted

GOOGLE_THROTTLE = ('{"error": {"code": 429, "message": "Resource has been exhausted (e.g. check quota).", '
                   '"status": "RESOURCE_EXHAUSTED"}}')
GOOGLE_PER_MINUTE = ('{"error": {"code": 429, "message": "You exceeded your current quota, please check your plan '
                     'and billing details. For more information on this error, head to: '
                     'https://ai.google.dev/gemini-api/docs/rate-limits.", "status": "RESOURCE_EXHAUSTED", '
                     '"details": [{"violations": [{"quotaId": '
                     '"GenerateContentPaidTierInputTokensPerModelPerMinute"}]}]}}')
GOOGLE_PER_DAY = GOOGLE_PER_MINUTE.replace("PaidTierInputTokensPerModelPerMinute", "RequestsPerDayPerProjectPerModel")
OPENAI_SPENT = ('{"error": {"message": "You exceeded your current quota, please check your plan and billing details. '
                'For more information on this error, read the docs: '
                'https://platform.openai.com/docs/guides/error-codes/api-errors.", "type": "insufficient_quota", '
                '"code": "insufficient_quota"}}')
CEREBRAS_TOKENS = ('{"message": "Tokens per minute limit exceeded - too many tokens processed.", '
                   '"type": "too_many_tokens_error", "code": "token_quota_exceeded"}')
ANTHROPIC_LIMIT = ('{"type": "error", "error": {"type": "rate_limit_error", "message": "This request would exceed '
                   'the rate limit for your organization of 400,000 input tokens per minute."}}')
OPENROUTER_UPSTREAM = ('{"error": {"message": "Provider returned error", "code": 429, "metadata": {"raw": '
                       '"deepseek/deepseek-flash is temporarily rate-limited upstream."}}}')
OPENROUTER_CREDITS = '{"error": {"message": "Insufficient credits. Add more using https://openrouter.ai/settings"}}'


@pytest.mark.parametrize("body, spent", [
    (GOOGLE_THROTTLE, False),
    (GOOGLE_PER_MINUTE, False),
    (GOOGLE_PER_DAY, True),
    (OPENAI_SPENT, True),
    (CEREBRAS_TOKENS, False),
    (ANTHROPIC_LIMIT, False),
    (OPENROUTER_UPSTREAM, False),
    (OPENROUTER_CREDITS, True),
    # Google words its per-minute limit like a cap; the wait it names tells them apart
    ("You exceeded your current quota, please check your plan and billing details. Please retry in 23.4s.", False),
    ("You exceeded your current quota, please check your plan and billing details. Please retry in 41866.2s.", True),
    ("You have used all available credits or reached your monthly spending limit.", True),   # xAI wording
    ("", False),
    (None, False),
])
def test_an_account_cap_is_told_from_a_throttle(body, spent):
    assert quota_exhausted(body) is spent
