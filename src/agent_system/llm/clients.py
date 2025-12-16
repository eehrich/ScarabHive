"""Compatibility shim for LLM clients.

This module preserves the old public API (`make_llm`) while the
implementations live in separate modules for OpenAI and Ollama.
"""

from __future__ import annotations

from typing import Optional, Any
import logging

from .models import LLMClient

# Import concrete implementations
from .openai_client import OpenAIAsyncClient  # type: ignore
from .ollama_client import OllamaNativeAsyncClient  # type: ignore
from .httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig  # type: ignore
from ..config.models import ModelCapabilitiesConfig


def make_llm(provider: str, model: str, openai_api_key: Optional[str], base_url: Optional[str] = None, ollama_url: Optional[str] = None, context_window: Optional[int] = None, ollama_mode: Optional[str] = None, request_timeout: Optional[int] = None, ssl_verify: Optional[bool] = None, client_type: Optional[str] = None, httpx_timeouts: Optional[dict] = None, capabilities: Optional[ModelCapabilitiesConfig] = None, parallel_tool_calls: bool = True) -> LLMClient:
    """Factory creating an async LLM client.

    - provider=openai: use AsyncOpenAI against OpenAI API.
    - provider=openai_httpx: use HTTPX-based OpenAI client.
    - provider=ollama: use Ollama (native or openai-compat) depending on mode.
    """
    import os
    logger = logging.getLogger(__name__)
    try:
        logger.debug("make_llm called provider=%s model=%s openai_key_set=%s base_url=%s ollama_url=%s ollama_mode=%s request_timeout=%s ssl_verify=%s client_type=%s httpx_timeouts=%s", provider, model, bool(openai_api_key), base_url, ollama_url, ollama_mode, request_timeout, ssl_verify, client_type, httpx_timeouts)
    except Exception:
        pass

    if provider == "openai" or provider == "openai_httpx":
        if not openai_api_key:
            openai_api_key = os.getenv("OPENAI_API_KEY")
        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when provider=openai")

        if provider == "openai_httpx":
            if httpx_timeouts:
                timeout_config = HTTPXTimeoutConfig(
                    connect=httpx_timeouts.get('connect', 10.0),
                    read=httpx_timeouts.get('read', float(request_timeout) if request_timeout else 180.0),
                    write=httpx_timeouts.get('write', 10.0),
                    pool=httpx_timeouts.get('pool', 5.0)
                )
            else:
                timeout_config = HTTPXTimeoutConfig(
                    connect=10.0,
                    read=float(request_timeout) if request_timeout else 180.0,
                    write=10.0,
                    pool=5.0
                )
            return HTTPXOpenAIClient(
                model=model,
                api_key=openai_api_key,
                base_url=base_url or "https://api.openai.com/v1",
                timeout_config=timeout_config,
                max_retries=3,
                retry_backoff=1.0,
                verify=ssl_verify,
                context_window=context_window,
                capabilities=capabilities,
                parallel_tool_calls=parallel_tool_calls
            )
        else:
            return OpenAIAsyncClient(
                model=model,
                api_key=openai_api_key,
                base_url=base_url or "https://api.openai.com/v1",
                timeout=float(request_timeout) if request_timeout else None,
                verify=ssl_verify,
                context_window=context_window,
                capabilities=capabilities
            )

    if provider == "ollama":
        mode = (ollama_mode or "openai_compat").lower()
        if mode == "native":
            base_native = (ollama_url.rstrip("/")) if ollama_url else "http://127.0.0.1:11434"
            options: dict[str, Any] = {}
            if context_window:
                options["num_ctx"] = context_window
            return OllamaNativeAsyncClient(
                model=model,
                base_url=base_native,
                options=options or None,
                timeout=float(request_timeout) if request_timeout else None,
                verify=ssl_verify,
                context_window=context_window,
                capabilities=capabilities
            )
        # For openai_compat mode: prefer base_url if set, otherwise fall back to ollama_url
        if base_url:
            base = base_url.rstrip("/")
        elif ollama_url:
            base = (ollama_url.rstrip("/") + "/v1")
        else:
            base = "http://127.0.0.1:11434/v1"
        
        api_key = openai_api_key or "ollama"
        return OpenAIAsyncClient(
            model=model,
            api_key=api_key,
            base_url=base,
            timeout=float(request_timeout) if request_timeout else None,
            verify=ssl_verify,
            context_window=context_window,
            capabilities=capabilities
        )

    raise ValueError(f"Unknown LLM provider: {provider}")
