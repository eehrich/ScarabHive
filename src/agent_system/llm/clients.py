"""Compatibility shim for LLM clients.

This module preserves the old public API (`make_llm`) while the
implementations live in separate modules for OpenAI, Ollama, Gemini, and Anthropic.
"""

from __future__ import annotations

from typing import Optional, Any
import logging

from .models import LLMClient

# Import concrete implementations
from .openai_client import OpenAIAsyncClient  # type: ignore
from .ollama_client import OllamaNativeAsyncClient  # type: ignore
from .httpx_client import HTTPXOpenAIClient, HTTPXTimeoutConfig  # type: ignore
from .gemini_client import GeminiClient  # type: ignore
from .gemini_sdk_client import GeminiSDKClient  # type: ignore
from .anthropic_client import AnthropicAsyncClient  # type: ignore
from ..config.models import ModelCapabilitiesConfig

logger = logging.getLogger(__name__)


def make_llm(provider: str, model: str, api_key: Optional[str], base_url: Optional[str] = None, context_window: Optional[int] = None, ollama_mode: Optional[str] = None, request_timeout: Optional[int] = None, ssl_verify: Optional[bool] = None, client_type: Optional[str] = None, httpx_timeouts: Optional[dict] = None, capabilities: Optional[ModelCapabilitiesConfig] = None, parallel_tool_calls: bool = True, include_thoughts: Optional[bool] = None, thinking_budget: Optional[int] = None, thinking_level: Optional[str] = None, max_tokens: Optional[int] = None, enable_prompt_caching: Optional[bool] = None, modalities: Optional[list[str]] = None, safety_settings: Optional[dict[str, str]] = None, service_tier: Optional[str] = None, provider_routing: Optional[dict] = None, reasoning_details_mode: Optional[str] = None, prompt_cache_key: Optional[str] = None, prompt_cache_mode: Optional[str] = None, prompt_cache_marker_style: Optional[str] = None) -> LLMClient:
    """Factory creating an async LLM client.

    - provider=openai: use AsyncOpenAI against OpenAI API.
    - provider=openai_httpx: use HTTPX-based OpenAI client.
    - provider=gemini: use HTTP-based Google Gemini API.
    - provider=gemini_sdk: use official Google Gen AI SDK.
    - provider=anthropic: use official Anthropic SDK for Claude models.
    - provider=ollama: use Ollama (native or openai-compat) depending on mode.
    """
    import os
    logger = logging.getLogger(__name__)
    try:
        logger.debug("make_llm called provider=%s model=%s api_key_set=%s base_url=%s ollama_mode=%s request_timeout=%s ssl_verify=%s client_type=%s httpx_timeouts=%s", provider, model, bool(api_key), base_url, ollama_mode, request_timeout, ssl_verify, client_type, httpx_timeouts)
    except Exception:
        pass

    if provider == "anthropic":
        if not api_key:
            api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when provider=anthropic")
        
        return AnthropicAsyncClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            context_window=context_window or 200000,
            request_timeout=request_timeout or 180,
            max_retries=3,
            max_tokens=max_tokens or 8192,
            include_thinking=include_thoughts or False,
            thinking_budget=thinking_budget,
            enable_prompt_caching=enable_prompt_caching if enable_prompt_caching is not None else True,
            prompt_cache_mode=prompt_cache_mode,
            reasoning_details_mode=reasoning_details_mode,
        )

    if provider == "gemini":
        if not api_key:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is required when provider=gemini")
        
        return GeminiClient(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
            context_window=context_window or 200000,
            request_timeout=request_timeout or 180,
            ssl_verify=ssl_verify if ssl_verify is not None else True,
            httpx_timeouts=httpx_timeouts,
            parallel_tool_calls=parallel_tool_calls,
            include_thoughts=include_thoughts,
            thinking_budget=thinking_budget,
            thinking_level=thinking_level,
            max_tokens=max_tokens,
            safety_settings=safety_settings,
        )
    
    if provider == "gemini_sdk":
        if not api_key:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is required when provider=gemini_sdk")
        
        return GeminiSDKClient(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://generativelanguage.googleapis.com/v1beta",
            context_window=context_window or 200000,
            request_timeout=request_timeout or 180,
            ssl_verify=ssl_verify if ssl_verify is not None else True,
            httpx_timeouts=httpx_timeouts,
            max_retries=3,
            parallel_tool_calls=parallel_tool_calls,
            include_thoughts=include_thoughts,
            thinking_budget=thinking_budget,
            thinking_level=thinking_level,
            max_tokens=max_tokens,
            safety_settings=safety_settings,
        )

    if provider == "openai_responses":
        # OpenAI Responses API via OpenRouter (/responses beta): native item
        # round-trip, no Chat-Completions bridging — removes the
        # encrypted-reasoning 400s of that translation layer entirely.
        # See openai_responses_client.py for the full rationale.
        if not api_key:
            api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("An API key is required when provider=openai_responses")
        from .openai_responses_client import OpenAIResponsesClient
        if httpx_timeouts:
            responses_timeouts = HTTPXTimeoutConfig(
                connect=httpx_timeouts.get('connect', 10.0),
                read=httpx_timeouts.get('read', float(request_timeout) if request_timeout else 600.0),
                write=httpx_timeouts.get('write', 30.0),
                pool=httpx_timeouts.get('pool', 5.0)
            )
        else:
            responses_timeouts = HTTPXTimeoutConfig(
                connect=10.0,
                read=float(request_timeout) if request_timeout else 600.0,
                write=30.0,
                pool=5.0
            )
        return OpenAIResponsesClient(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://openrouter.ai/api/v1",
            context_window=context_window or 200000,
            request_timeout=request_timeout or 600,
            ssl_verify=ssl_verify if ssl_verify is not None else True,
            timeout_config=responses_timeouts,
            capabilities=capabilities,
            parallel_tool_calls=parallel_tool_calls,
            thinking_level=thinking_level,
            max_tokens=max_tokens,
            service_tier=service_tier,
            provider_routing=provider_routing,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_mode=prompt_cache_mode,
            prompt_cache_marker_style=prompt_cache_marker_style,
        )

    if provider == "openai" or provider == "openai_httpx":
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
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
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                timeout_config=timeout_config,
                max_retries=1,  # 2 attempts total — faster fallback on 5xx (e.g. DeepSeek 504)
                retry_backoff=1.0,
                verify=ssl_verify,
                context_window=context_window,
                capabilities=capabilities,
                parallel_tool_calls=parallel_tool_calls,
                max_tokens=max_tokens,
                thinking_level=thinking_level,
                thinking_budget=thinking_budget,
                safety_settings=safety_settings,
                service_tier=service_tier,
                provider_routing=provider_routing,
                reasoning_details_mode=reasoning_details_mode,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_mode=prompt_cache_mode,
                prompt_cache_marker_style=prompt_cache_marker_style,
            )
        else:
            # Build default_extra dict for additional parameters
            default_extra: dict[str, Any] = {}
            if modalities:
                default_extra["modalities"] = modalities

            if prompt_cache_key or prompt_cache_mode:
                # Sichtbar statt still verworfen (Review-Finding): der
                # SDK-Client hat keinen prompt_cache_key-Pfad — wer Caching
                # keyen will, muss auf openai_httpx/openai_responses.
                logger.warning(
                    "prompt_cache_key/prompt_cache_mode sind fuer "
                    "provider=openai (SDK-Client) nicht verdrahtet und werden "
                    "ignoriert (model=%s) — Profil auf openai_httpx oder "
                    "openai_responses umstellen.",
                    model,
                )

            return OpenAIAsyncClient(
                model=model,
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                timeout=float(request_timeout) if request_timeout else None,
                verify=ssl_verify,
                context_window=context_window,
                capabilities=capabilities,
                default_extra=default_extra if default_extra else None
            )

    if provider == "ollama":
        mode = (ollama_mode or "openai_compat").lower()
        if mode == "native":
            base_native = (base_url.rstrip("/")) if base_url else "http://127.0.0.1:11434"
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
        # For openai_compat mode: use base_url or default
        if base_url:
            base = base_url.rstrip("/")
        else:
            base = "http://127.0.0.1:11434/v1"
        
        key = api_key or "ollama"
        if prompt_cache_key:
            logger.warning(
                "prompt_cache_key wird fuer provider=ollama ignoriert (model=%s).",
                model,
            )
        return OpenAIAsyncClient(
            model=model,
            api_key=key,
            base_url=base,
            timeout=float(request_timeout) if request_timeout else None,
            verify=ssl_verify,
            context_window=context_window,
            capabilities=capabilities
        )

    raise ValueError(f"Unknown LLM provider: {provider}")
