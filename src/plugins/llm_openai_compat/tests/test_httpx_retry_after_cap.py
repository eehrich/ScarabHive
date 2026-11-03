"""A server's Retry-After must not park a turn: 3600 is capped, inf is no hint."""

def test_httpx_client_caps_the_wait():
    from plugins.llm_openai_compat.httpx_client import HTTPXOpenAIClient, MAX_RETRY_AFTER_SECONDS

    parse = HTTPXOpenAIClient._parse_retry_after
    assert parse(None, "3600") == MAX_RETRY_AFTER_SECONDS
    assert parse(None, "inf") is None
    assert parse(None, "2") == 2.0
