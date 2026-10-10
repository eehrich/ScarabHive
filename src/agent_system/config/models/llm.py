"""The ``llm_system:`` section (config/llm.yaml): chat models and their profiles, the batch
system, TTS and decision models with theirs, and LLMSystemConfig, which checks all of them
against the provider plugins when the config loads.

Its own module because it is the one section whose vocabulary belongs to someone else: the
provider plugins' manifests name the providers, and every model entry is checked against them.
"""
from __future__ import annotations

import logging
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal, Optional, Dict, List, Any


# ===========================
# LLM Configuration Models
# ===========================

class HTTPXTimeoutConfig(BaseModel):
    """HTTPX timeout configuration"""
    connect: float = 10.0      # Connection establishment timeout
    read: float = 180.0        # Read timeout (waiting for response data)
    write: float = 10.0        # Write timeout (sending request data)
    pool: float = 5.0          # Pool timeout (getting connection from pool)


class ModelCapabilitiesConfig(BaseModel):
    """What a model can take and produce.

    THE definition — llm.capabilities.ModelCapabilities derives from this
    class instead of repeating it. It used to be a second copy, field for
    field, and a new capability had to be added twice.
    """
    # An unknown key here is a claim about the model that never arrives.
    model_config = ConfigDict(extra="forbid")

    tools: bool = True
    function_calling: bool = True
    image_input: bool = False
    audio_input: bool = False
    video_input: bool = False
    streaming: bool = True
    # JSON mode ("any JSON object"). NOT READ: structured output sends native JSON
    # mode only to a model declaring structured_output below -- the catalogue's
    # json_mode values were never verified, and Gemini before 3 refuses JSON mode
    # beside tools just as it refuses a schema there.
    json_mode: bool = False
    # The backend constrains the answer to a caller's JSON schema, and takes plain
    # JSON mode -- ALSO in a request that carries tools, since an agent asks with
    # its tools on every step (Gemini before 3 refuses that combination: leave it
    # false there). Unset means no: a structured request to this model fails with
    # StructuredOutputUnsupported, or falls back to prompt + validation if the
    # caller allows it (openai_api always does).
    structured_output: bool = False

    # API type support (OpenAI specific)
    supported_api_types: Optional[List[str]] = None  # e.g. ['chat_completions', 'realtime']
    default_api_type: Optional[str] = None  # e.g. 'realtime'

    # Image input limits
    max_image_size: Optional[int] = None  # bytes
    max_image_resolution: Optional[List[int]] = None  # [width, height]
    min_image_resolution: Optional[List[int]] = None  # [width, height]
    supported_image_formats: List[str] = Field(default_factory=list)
    image_detail_control: bool = False

    # Audio input limits
    max_audio_size: Optional[int] = None  # bytes
    max_audio_duration: Optional[int] = None  # seconds
    supported_audio_formats: List[str] = Field(default_factory=list)

    # Video input limits
    max_video_size: Optional[int] = None  # bytes
    max_video_duration: Optional[int] = None  # seconds
    supported_video_formats: List[str] = Field(default_factory=list)

    # Provider-specific features
    supports_files_api: bool = False
    supports_file_uploads: bool = False

    # Which rung of the developer-note ladder this model's BACKEND takes
    # (llm/message_roles.py): "developer", "system" or "user". None means the
    # route decides, which is right until an endpoint speaks a richer format
    # than the thing behind it -- an OpenAI-compatible host may be llama.cpp,
    # whose chat template silently renders an unknown role as nothing. A value
    # above what the wire format permits is refused by the client with a
    # warning, never sent.
    developer_role: Optional[Literal["developer", "system", "user"]] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_multimodal_shorthand(cls, data):
        """``multimodal:`` was a shorthand that only worked in one direction.

        True set all three input flags — overriding an explicit
        ``video_input: false`` — and False did nothing at all, while reading
        like a statement. Say which modality you mean.
        """
        if isinstance(data, dict) and "multimodal" in data:
            raise ValueError(
                "capabilities.multimodal was removed: set image_input / "
                "audio_input / video_input explicitly. `multimodal: true` "
                "meant all three, `multimodal: false` meant nothing.")
        return data


class BatchProviderConfig(BaseModel):
    """Configuration for a specific batch provider (Gemini/OpenAI).
    
    Batch APIs provide 50% cost reduction and separate rate limits
    for non-time-critical workloads. Requests are collected, submitted
    as batch jobs, and results are polled asynchronously.
    """
    enabled: bool = True  # Enable this batch provider
    collection_window_seconds: float = 10.0  # Time to collect requests before submitting batch
    max_requests_per_batch: int = 100  # Max requests per batch (OpenAI: 50k, Gemini: 200k)
    poll_interval_seconds: float = 10.0  # Interval between status polls
    max_wait_hours: float = 24.0  # Max time to wait for batch completion
    max_retries: int = 3  # Max retries for server-side cancelled jobs
    cancel_on_startup: bool = True  # Cancel orphaned batches on app startup
    fallback_to_sync: bool = False  # Fallback to sync API on timeout/failure


class BatchSystemConfig(BaseModel):
    """Global batch system configuration.

    Centralized configuration for batch processing. Models with
    provider='batch' reference this config via their batch_provider field.
    """
    storage_path: str = "data/batch_jobs"  # Where to store batch job data
    #: batch provider name -> its settings. A DICT, not a model with one
    #: field per provider: the batch vocabulary belongs to the plugins
    #: (plugin.toml `provides_batch`), and a fixed field list made the core a
    #: second registry that had to be edited for every new backend. The YAML
    #: shape is unchanged — `providers: {gemini: {...}, openai: {...}}` reads
    #: the same either way. Unknown names are caught by
    #: LLMSystemConfig._providers_must_exist_as_plugins.
    providers: Dict[str, BatchProviderConfig] = Field(default_factory=dict)


class LLMModelConfig(BaseModel):
    """Individual LLM model configuration"""
    # Same reason as AgentConfig: an unknown key here is silently dropped, and
    # since `extends` is resolved BEFORE validation, a leftover one would mean
    # the entry quietly runs on defaults instead of on its base.
    model_config = ConfigDict(extra="forbid")

    # A free string, NOT a Literal: the provider plugins under
    # src/plugins/ are the source of truth (their plugin.toml `provides`
    # lists). Typos still fail at config load — see
    # LLMSystemConfig._providers_must_exist_as_plugins. "batch" is the one
    # pseudo-provider: the resolver maps it to the real provider via
    # batch_provider before the registry ever sees it.
    provider: str = "ollama"
    # Optional so that an entry can be a pure base class: it collects
    # provider, base_url, limits and capabilities, and the children only set
    # the model name. An entry without one must not be used — that is
    # checked by LLMSystemConfig._profiles_must_point_at_usable_models.
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None  # Custom base URL for API endpoint (e.g. Gemini, Ollama, OpenAI-compatible)
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls
    stream_silence_timeout: Optional[float] = Field(None, gt=0)  # Seconds a stream may deliver only keep-alive comments (they reset the read timeout) before the attempt is retried; None = no limit of ours. See docs/llm_catalog.md.
    parallel_tool_calls: Optional[bool] = True  # Enable parallel tool execution (set to False if LLM concatenates tool names/args). None = leave the field out of the request, for a backend that refuses what it does not know. No catalogue entry needs that today; the value exists because "send true", "send false" and "say nothing" are three different requests.
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # HTTPX-specific timeout overrides
    capabilities: Optional[ModelCapabilitiesConfig] = None  # Model capabilities
    include_thoughts: Optional[bool] = None  # Enable thinking/reasoning output (Gemini, DeepSeek)
    enable_prompt_caching: Optional[bool] = None  # Anthropic prompt caching (client default: True). Was a dead key in llm.yaml before this field existed.
    thinking_budget: Optional[int] = None  # Token budget for thinking (Gemini 2.5: 1-24576, default 8192)
    thinking_level: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]] = None  # Thinking level = OpenRouter reasoning.effort enum (none|minimal|low|medium|high|xhigh|max — "ultra" existed here but no provider knows it). Gemini 3 only knows minimal-high (higher values are clamped to high in the Gemini clients); "none" disables thinking on hybrid models (OpenRouter: DeepSeek V4 & co.)
    modalities: Optional[List[str]] = None  # Output modalities for audio models (e.g., ["text"] or ["text", "audio"])
    max_tokens: Optional[int] = None  # Maximum output tokens (limits response length, reduces costs)
    temperature: Optional[float] = None  # Sampling temperature. None = provider default (~1.0 for DeepSeek/OpenAI!). Set it low for mechanical tasks (structure, mapping, extraction): measured 2026-07-25, all prose-converter calls ran on the provider default, i.e. full sampling variance for a copy task. ⚠️ Do NOT set it for reasoning models (gpt-5.x/o-series accept only temperature=1 or reject the param) — thinking_level controls those. Sent as a top-level field to OpenAI-compatible/Anthropic/Gemini APIs.
    safety_settings: Optional[Dict[str, str]] = None  # Gemini safety settings: {HarmCategory: HarmBlockThreshold}. Sent whenever it is set — a model whose backend does not know the field must not carry it.
    tool_schema_dialect: Optional[Literal["json_schema", "gemini_function_declarations"]] = None  # Which tool schema the endpoint accepts. "json_schema" (default) passes the schema as it is. "gemini_function_declarations" strips what Gemini's function declarations reject (additionalProperties, default, format, title, anyOf/oneOf, ...) — without it Gemini answers MALFORMED_FUNCTION_CALL. Declared per model because the same family is reachable through gateways that translate and gateways that do not. It also arms the detector for that decoder's other failure, a function call leaking into the text channel: a gateway that already translates (so "json_schema") cannot produce it, and a model merely writing the marker keeps its answer.
    assistant_reasoning_field: Optional[Literal["omit", "reasoning_content"]] = None  # What an assistant message carries back as its thinking, next to the standard reasoning_details. "omit" (default) sends the field to no one — it is not part of the OpenAI schema. "reasoning_content" fills it on EVERY assistant message, empty string included: DeepSeek's thinking mode answers 400 without it on a tool round trip.
    thinking_request_shape: Optional[Literal["budget", "adaptive"]] = None  # How this model wants its thinking asked for on the native Anthropic API: "budget" (default) sends {"type": "enabled", "budget_tokens": N}, "adaptive" sends {"type": "adaptive"} — the newer models reject budget_tokens with a 400, and the older ones do not know adaptive.
    service_tier: Optional[str] = None  # Service tier for OpenAI-compatible APIs (e.g. "flex" = Google Flex Processing via OpenRouter — cheaper, slower)
    prompt_cache_key: Optional[str] = None  # OpenAI cache routing key (Chat + Responses API). From GPT-5.6 on REQUIRED for reliable prompt-cache matching (docs: "you must set prompt_cache_key ... for both implicit and explicit caching"); without a key 5.6 practically never caches (shown 2026-07-21: byte-identical 10k prefix, cached_tokens=0). Recommended value "auto": the key is hashed per request from the leading system prompt (in full — a long system prompt must not eat up the window) + the first task message up to ~4096 characters (llm/cache_key.py) — same stable prefix <-> same key, stable across follow-up turns, collision-free for parallel jobs (a static agent-name key would make them share a shard + the ~15 req/min limit). A static string remains possible as an explicit override. Set only for OpenAI/OpenRouter GPT models — other providers might reject the param.
    prompt_cache_mode: Optional[Literal["auto", "multi_turn", "task_sequence", "one_shot", "off"]] = None  # Cache behaviour of the agent. Set in agent yamls via llm_params "*" (flat per-agent form — there is no AgentConfig->client path). "task_sequence" enables the segment ladder over declared sentinel boundaries; "multi_turn" documents continuation caching (no markers); "off" also strips sentinels. Default None ~ "auto" (mark declared boundaries only).
    prompt_cache_marker_style: Optional[Literal["openai", "anthropic", "none"]] = None  # Marker field of the model: "openai" = prompt_cache_breakpoint (GPT-5.6+), "anthropic" = cache_control ephemeral, "none" = strip sentinels. Default None = no marker unless prompt_cache_key is set (then "openai", because by config discipline the key only sits on GPT entries). A model that speaks cache_control says so HERE — the client knows no model names.
    provider_routing: Optional[Dict[str, Any]] = None  # OpenRouter "provider" object: {order: [slugs], allow_fallbacks: bool, sort: "price", ...}. Order-only is enough to bias toward a sticky backend (improves implicit cache hit rate); allow_fallbacks: false would hard-pin. System-wide default: llm_system.openrouter_routing (overridden per key from here). ⚠️ Only the paths provider=openai_httpx and openai_responses pass the field on to the request — the SDK path provider=openai does not know it and would SILENTLY drop it (no model in the catalogue uses it; whoever switches one over loses the routing without a word).
    provider_affinity_minutes: Optional[float] = Field(default=None, ge=0)  # How long after an agent type's last call a new run of it starts on the backend that answered that call (sent as provider.order [that one] with allow_fallbacks false; a refusal releases the pin for that call and the retry goes out as configured). None = prompt_cache_ttl_minutes, without that 30 min; 0 = off. The prompt a type's runs share is cached on that backend: measured 22.09.2026, a run's first call on it read 57.8 % from cache within 5 min of the previous call and 22.5 % within 30 min, on another backend 27.8 % and 9.0 %; past 30 min under 10 % either way. A run's own history (served_by) always wins. Only the OpenRouter routes (openai_httpx, openai_responses, openrouter_sdk) and only with provider_routing.order.
    prompt_cache_ttl_minutes: Optional[float] = Field(default=None, gt=0)  # How long the provider's prompt cache probably stays valid after its last write or hit, in minutes. Sent nowhere -- the system reads it: the backend pin (provider_affinity_minutes falls back to it) and whoever must judge whether a paused session's prefix is still cached. Anthropic 5 (its default ttl; 1 h would need a ttl in cache_control), GPT-5.6+/6 30 (fixed), Gemini implicit about 3-5. None = unknown.
    reasoning_details_mode: Optional[Literal["keep_last", "keep_all", "strip"]] = None  # How to round-trip provider reasoning blocks across turns: "keep_last" (default, Gemini — current turn's thought signature only), "keep_all" (encrypted chains that must stay intact, and every model whose cache matches the prefix byte for byte: stripping older blocks rewrites the prefix each turn and the cache is written anew instead of read — measured on book_launcher, ~26k tokens per step), "strip" (drop entirely). On the chat route keep_all also keeps the blocks of a turn whose chain was broken by a compaction WHILE its tools are still open, because that turn's thinking has to be echoed back complete (see utils/reasoning_artifacts); a model whose chain is encrypted and verified across turns wants the Responses route, where the chain reset stays unconditional. Literal: a typo must fail config load, not silently fall back to keep_last.
    plugins: Optional[List[Dict[str, Any]]] = None  # OpenRouter request plugins, passed through verbatim: [{"id": "context-compression", "engine": "middle-out"}, {"id": "response-healing"}, {"id": "moderation"}, {"id": "file-parser", "pdf": {...}}, {"id": "auto-router", ...}]. Free dicts for the same reason provider_routing is one — the vocabulary belongs to the gateway. UNSET by default: every one of them changes what the model sees or costs (context-compression rewrites the prompt), so none is turned on without a measurement behind it.
    prompt_cache_options: Optional[Dict[str, Any]] = None  # OpenRouter/OpenAI request-level cache controls, e.g. {"mode": "explicit"} — disables OpenAI-MANAGED breakpoints so only blocks carrying prompt_cache_breakpoint are cached (GPT-5.6+ only). Unset means the provider keeps its automatic breakpoints IN ADDITION to ours; which of the two caches better is unmeasured here, so this stays a deliberate per-model switch.
    safety_identifier: Optional[str] = None  # Stable per-end-user pseudonym for abuse isolation (Responses route only — the chat route has no such field). Without it a request carries the ACCOUNT identity, so a provider policy block can hit every model at once. A static value per model entry gives the app one identity; isolating individual runs would need a value from the caller, which no client path supplies today.

    # Batch provider (only for provider="batch"). Free string for the same
    # reason `provider` is one: the vocabulary belongs to the plugins
    # (plugin.toml `provides_batch`), and a Literal here would make the core
    # a second registry that has to be edited for every new batch backend.
    # Typos are caught by _providers_must_exist_as_plugins against
    # registry.known_batch_providers().
    batch_provider: Optional[str] = None  # Which batch API to use


class LLMProfile(BaseModel):
    """LLM usage profile that references a model"""
    model_ref: str  # Reference to model in models dict
    description: Optional[str] = None
    max_steps: Optional[int] = None


# ===========================
# TTS Configuration Models
# ===========================

class TTSModelConfig(BaseModel):
    """Individual TTS model configuration.
    
    Defines a TTS backend (e.g. Gemini TTS) with its connection and
    generation parameters.
    """
    # Free string like LLMModelConfig.provider: the plugins under
    # src/plugins/ own the vocabulary via `provides_tts` in their
    # manifests; typos fail at config load through the same
    # LLMSystemConfig validator that guards LLM providers.
    provider: str = "gemini_tts"
    model: str  # e.g. "gemini-2.5-flash-preview-tts"
    api_key: Optional[str] = None  # provider factory falls back to its env vars
    base_url: Optional[str] = None  # openai_speech: api.openai.com vs openrouter.ai (default OpenRouter)
    voice: Optional[str] = None  # default voice when the caller passes none (openai_speech requires one)
    request_timeout: int = 300  # TTS can be slow for long texts
    max_retries: int = Field(3, ge=0)  # negative would silently skip every attempt


class TTSProfile(BaseModel):
    """Named TTS profile that references a TTS model.

    No voice here: the default voice lives on the MODEL entry
    (TTSModelConfig.voice) — a profile-level default_voice existed once,
    was read by nobody, and only looked like configuration.
    """
    model_ref: str  # Reference to key in tts_models dict
    description: Optional[str] = None


# ===========================
# Decision Model Configuration
# ===========================

class DecisionModelConfig(BaseModel):
    """Individual decision model configuration.

    A decision model answers NAMED QUESTIONS about a piece of content with a
    typed value and a probability — no message list, no prose, no tool calls
    (TypeSafe's Jev behind OpenRouter's ``/api/alpha/decisions`` is the first).
    It therefore does not belong in ``models``: nothing here can serve
    ``chat()``, and an entry there would be offered to every agent as a chat
    model and checked for a per-token price it does not have. OpenRouter's
    answer carries its cost; one that carries none (a local Laya, TypeSafe
    direct) is priced by the usage tracker from llm_pricing.yaml, by model.

    The shape of ``TTSModelConfig`` minus its voice, for the same reason: a
    non-chat client needs a connection and nothing from the chat knobs.
    """
    # extra="forbid", like LLMModelConfig and unlike the TTS twin. This
    # section is the one whose endpoint key is NOT called base_url, so the
    # habit from two sections up writes a key this model does not know — and
    # dropping it silently would send the request to OpenRouter, carrying the
    # OpenRouter key, instead of to the proxy the operator meant. The profile
    # that used to live under `profiles:` also carried `max_steps: 500`, so
    # copying the old entry over hits this too.
    model_config = ConfigDict(extra="forbid")
    # Free string like TTSModelConfig.provider: the plugins under
    # src/plugins/ own the vocabulary via `provides_decisions` in their
    # manifests; a typo fails at config load through the same
    # LLMSystemConfig validator that guards LLM and TTS providers.
    provider: str = "openrouter_decisions"
    model: str  # e.g. "~typesafe/jev-latest"
    api_key: Optional[str] = None  # the client falls back to the env var of the endpoint's host
    # NOT called base_url like its neighbours, because it is not one: nothing
    # appends a path to it. The whole endpoint goes here, and the API key is
    # resolved against ITS host. None = the provider's own default.
    url: Optional[str] = None
    request_timeout: int = 60  # a decision is one short call, not a generation
    max_retries: int = Field(2, ge=0)  # negative would silently skip every attempt


class DecisionProfile(BaseModel):
    """Named decision profile that references a decision model."""
    model_config = ConfigDict(extra="forbid")  # same reason as above
    model_ref: str  # Reference to key in decision_models dict
    description: Optional[str] = None


class LLMSystemConfig(BaseModel):
    """Complete LLM system configuration"""
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # Default HTTPX timeouts for all models
    openrouter_routing: Optional[Dict[str, Any]] = None  # System default for the OpenRouter "provider" object (see LLMModelConfig.provider_routing). Applies to EVERY model with an openrouter.ai base_url; per key the model entry wins, so a deliberately set order/sort on the model stays. {sort: price} picks the cheapest provider (identical to the model suffix ":floor"; allowed: "price", "throughput", "latency"). ⚠️ sort switches off the OpenRouter load balancing and dilutes the stickiness of the order entries that keep the implicit prompt cache warm here — on long contexts a cache miss easily costs more than the cheaper provider saves. Alternative without this side effect: max_price as a ceiling. Default None = off.
    batch: Optional[BatchSystemConfig] = None  # Global batch processing configuration
    models: Dict[str, LLMModelConfig] = {}
    profiles: Dict[str, LLMProfile] = {}
    default_profile: Optional[str] = "normal"  # Default LLM profile to use

    @field_validator("profiles")
    @classmethod
    def _profile_names_must_not_shadow_model_fields(
        cls, v: Dict[str, "LLMProfile"]
    ) -> Dict[str, "LLMProfile"]:
        # Profile names are the keys of the profile-keyed agent_config.llm_params —
        # the form detection (flat vs. keyed) tells param names apart from
        # profile names. A profile named like an LLMModelConfig field
        # (e.g. "max_tokens") could not be addressed there → forbid it at the
        # root instead of silently misreading it later.
        shadowed = set(v) & set(LLMModelConfig.model_fields)
        if shadowed:
            raise ValueError(
                f"llm_system.profiles: profile names {sorted(shadowed)} collide "
                f"with LLMModelConfig field names (reserved for llm_params) — "
                f"please rename"
            )
        return v

    @model_validator(mode="after")
    def _profiles_must_point_at_usable_models(self) -> "LLMSystemConfig":
        """A base class without `model:` is there to inherit from, not to run.

        Without this guard its None ends up in the client and the call fails
        only at the provider — with a message that has nothing to do with the
        config.
        """
        broken = sorted(
            f"{name} -> {profile.model_ref}"
            for name, profile in self.profiles.items()
            if profile.model_ref in self.models
            and not self.models[profile.model_ref].model
        )
        if broken:
            raise ValueError(
                "llm_system.profiles: these profiles point at entries without "
                f"`model:` — those are base classes meant for inheriting: {broken}")
        return self

    @model_validator(mode="after")
    def _providers_must_exist_as_plugins(self) -> "LLMSystemConfig":
        """A typo in `provider:` must fail at config load, not at first use.

        The provider vocabulary is owned by the plugins under
        src/plugins/ (their manifests' `provides` lists) — this used to
        be a Literal here, which made the core the second registry. "batch"
        is the resolver-internal pseudo-provider and always allowed.

        Lenient on a missing/unreadable plugin root: config loading must not
        die because the scan failed — the registry raises its own, clearer
        error at build time in that case.
        """
        try:
            from agent_system.llm.registry import (
                batch_client_provider, known_batch_providers,
                known_decisions_providers, known_providers,
                known_tts_providers)
            known = known_providers()
            known_tts = known_tts_providers()
            known_decisions = known_decisions_providers()
        except Exception as e:
            # Lenient, but never silent: without this line an unreadable
            # plugin root turns the typo guard off with no trace, and every
            # agent dies individually at build time instead. Names EVERY
            # check — the TTS half used to be skipped without mention.
            logging.getLogger(__name__).warning(
                "LLM, TTS and decision provider validation skipped (manifest "
                "scan failed): %s", e)
            return self
        unknown = sorted(
            f"{name} (provider={m.provider})"
            for name, m in self.models.items()
            if m.provider != "batch" and m.provider not in known
        )
        if unknown:
            raise ValueError(
                f"llm_system.models: unknown provider on {unknown} — no plugin "
                f"under src/plugins declares it (known: {sorted(known)})")

        # provider: batch is the resolver's pseudo-provider — the REAL work is
        # done by the provider batch_provider names. Without this the typo
        # guard has a hole exactly where the config is least obvious: a batch
        # entry with a missing or misspelled batch_provider loaded fine and
        # died at the first agent build, which is what this validator exists
        # to prevent. Both the batch vocabulary and the client it maps to come
        # from the plugin manifests.
        broken_batch = []
        for name, m in self.models.items():
            if m.provider != "batch":
                continue
            if not m.batch_provider:
                broken_batch.append(f"{name} (no batch_provider)")
                continue
            try:
                # Also rejects an unknown batch_provider, naming the declared
                # ones — no second copy of that check here.
                client_provider = batch_client_provider(m.batch_provider)
            except ValueError as e:
                broken_batch.append(f"{name}: {e}")
                continue
            if client_provider not in known:
                broken_batch.append(
                    f"{name} (batch_provider={m.batch_provider} needs provider "
                    f"'{client_provider}', which no plugin declares)")
        if broken_batch:
            raise ValueError(
                f"llm_system.models: unusable batch models {sorted(broken_batch)}")

        # The keys of batch.providers are the same vocabulary — and the only
        # part of it nothing checked: a typo there is accepted and silently
        # ignored, so the provider runs on hardcoded defaults instead of its
        # configured collection window, poll interval, and cancel_on_startup.
        if self.batch and self.batch.providers:
            unknown_batch_cfg = sorted(
                name for name in self.batch.providers
                if name not in known_batch_providers())
            if unknown_batch_cfg:
                raise ValueError(
                    f"llm_system.batch.providers: unknown batch provider on "
                    f"{unknown_batch_cfg} — no plugin under src/plugins "
                    f"declares it via provides_batch "
                    f"(known: {sorted(known_batch_providers())})")

        # A profile pointing at no model fails at the first USE of it: a decision
        # inside an agent loop (agent_continuation asks one per step), a synthesis for
        # TTS. Both are loud and both are late, and a typo in a config file is cheapest
        # at config load. Measured over the shipped config, none of the 3 + 1 profiles
        # in these two sections dangles, so this refuses nothing that works today.
        #
        # NOT `profiles` (the chat ones), although the mistake is the same there. That
        # section has a deliberate degrade-and-name design behind it: an agent whose
        # profile does not resolve is built anyway with llm=None and logs a warning
        # carrying its own name, because on 2026-09-05 one model removed from llm.yaml
        # produced 50 warnings nobody could tell apart (see
        # test_a_swallowed_llm_error_names_the_agent). Refusing the config instead
        # would take the whole API down over one typo in one of 83 profiles, mid book
        # run — that is a trade to be decided, not to be slipped in here.
        # Both checks walk the same two sections, so they walk them together.
        # A fifth non-chat seam is one row here rather than two more blocks —
        # which is how the decisions seam got a provider check and no profile
        # check on its first day.
        for kind, label, models, profiles, known_names, manifest_key in (
            ("tts", "TTS", self.tts_models, self.tts_profiles,
             known_tts, "provides_tts"),
            ("decision", "decisions", self.decision_models, self.decision_profiles,
             known_decisions, "provides_decisions"),
        ):
            unknown = sorted(
                f"{name} (provider={m.provider})"
                for name, m in (models or {}).items()
                if m.provider not in known_names
            )
            if unknown:
                raise ValueError(
                    f"llm_system.{kind}_models: unknown {label} provider on "
                    f"{unknown} — no plugin under src/plugins declares it "
                    f"via {manifest_key} (known: {sorted(known_names)})")

            dangling = sorted(
                f"{name} -> {profile.model_ref}"
                for name, profile in (profiles or {}).items()
                if profile.model_ref not in (models or {})
            )
            if dangling:
                raise ValueError(
                    f"llm_system.{kind}_profiles: {dangling} name no entry in "
                    f"{kind}_models (have: {sorted(models or {})})")

        default_profile = self.default_decision_profile
        if default_profile and default_profile not in self.decision_profiles:
            raise ValueError(
                f"llm_system.default_decision_profile is {default_profile!r}, "
                f"which is no decision profile "
                f"(have: {sorted(self.decision_profiles)})")
        return self

    # TTS (Text-to-Speech) configuration. No default_tts_profile: it was set in
    # llm.yaml, mirrored into three schemas and asserted by a test, and read by
    # nothing in the repo — the test only checked that pydantic returned the value it
    # had been handed three lines above, which is what let it look like configuration
    # for a year. Every caller names the profile it wants.
    tts_models: Dict[str, TTSModelConfig] = {}
    tts_profiles: Dict[str, TTSProfile] = {}

    # Decision models — see DecisionModelConfig: a questionnaire, not a chat.
    decision_models: Dict[str, DecisionModelConfig] = {}
    decision_profiles: Dict[str, DecisionProfile] = {}
    #: Which profile a caller gets when it names none. This one has a reader from
    #: the start: create_decisions_from_profile falls back to it. Its TTS twin had
    #: none and has been removed — a default nothing reads is not configuration, it
    #: is decoration, so if this ever loses its reader it should go the same way.
    default_decision_profile: Optional[str] = None
