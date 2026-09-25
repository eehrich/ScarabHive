"""Anti-drift: the retry list lives in ONE place.

It was written out twice, in two clients that both talk to OpenRouter, and
when Cloudflare's 520 turned out to be missing it was missing in both. A
second copy is how that happens again, so this asks each client which object
it uses rather than comparing values -- an equal-looking local copy would pass
a value comparison and drift the next time one of them is edited.
"""
from plugins.llm_common.http_status import RETRYABLE_STATUS


def test_every_client_shares_the_one_retry_list():
    from plugins.llm_decisions import system_one
    from plugins.llm_openai_compat import openai_speech_client

    for module in (system_one, openai_speech_client):
        assert module.RETRYABLE_STATUS is RETRYABLE_STATUS, (
            f"{module.__name__} carries its own retry list again")


def test_the_transient_cloudflare_codes_are_in_it():
    """520-524 are Cloudflare's, not the origin's: always worth another try."""
    assert {520, 521, 522, 523, 524} <= RETRYABLE_STATUS
    assert 400 not in RETRYABLE_STATUS and 401 not in RETRYABLE_STATUS, (
        "a refusal repeated is only time burned")
