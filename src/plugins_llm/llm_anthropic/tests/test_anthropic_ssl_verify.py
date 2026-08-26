"""`network.ssl_verify: false` has to reach this provider too.

Every other TLS provider (openai, openai_httpx, openai_responses, gemini,
ollama) wires the flag through. The Anthropic factory accepted it and threw
it away, so behind a TLS-intercepting proxy this one provider kept failing
on the certificate with nothing pointing at the setting.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[3]))

from agent_system.config.models import LLMModelConfig
from plugins_llm.llm_anthropic.provider import build_anthropic


def _build(ssl_verify):
    with patch("anthropic.AsyncAnthropic") as sdk:
        build_anthropic(
            LLMModelConfig(provider="anthropic", model="claude-sonnet-4-6",
                           api_key="sk-test"),
            ssl_verify=ssl_verify)
    return sdk.call_args.kwargs


def test_ssl_verify_false_reaches_the_sdk():
    kwargs = _build(False)
    http_client = kwargs.get("http_client")
    assert http_client is not None, "ssl_verify=False was dropped"
    # httpx keeps the verify setting on the transport's SSL context.
    assert http_client._transport._pool._ssl_context.verify_mode.name == "CERT_NONE"


def test_the_default_keeps_verification_on():
    """Counter-check: nothing may switch verification off implicitly."""
    assert "http_client" not in _build(None)
    assert "http_client" not in _build(True)
