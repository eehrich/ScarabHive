"""
Configuration models for AgentSystem.

This module provides Pydantic models that match the new YAML configuration
structure with config.yaml as the master configuration and included files
for LLM and MCP configurations.
"""
from __future__ import annotations

import logging
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationInfo, model_validator
from typing import Literal, Optional, Dict, List, Any, Union


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
    json_mode: bool = False

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
    # src/plugins_llm/ are the source of truth (their plugin.toml `provides`
    # lists). Typos still fail at config load — see
    # LLMSystemConfig._providers_must_exist_as_plugins. "batch" is the one
    # pseudo-provider: the resolver maps it to the real provider via
    # batch_provider before the registry ever sees it.
    provider: str = "ollama"
    # Optional, damit ein Eintrag reine Basisklasse sein kann: er sammelt
    # provider, base_url, Limits und capabilities, und die Kinder setzen nur
    # noch den Modellnamen. Wer keinen hat, darf nicht benutzt werden — das
    # prueft LLMSystemConfig._profiles_must_point_at_usable_models.
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None  # Custom base URL for API endpoint (e.g. Gemini, Ollama, OpenAI-compatible)
    context_window: int = 32768  # default num_ctx for Ollama-compatible models
    ollama_mode: Literal["openai_compat", "native"] = "openai_compat"
    request_timeout: int = 120  # seconds for LLM API calls
    parallel_tool_calls: bool = True  # Enable parallel tool execution (set to False if LLM concatenates tool names/args)
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # HTTPX-specific timeout overrides
    capabilities: Optional[ModelCapabilitiesConfig] = None  # Model capabilities
    include_thoughts: Optional[bool] = None  # Enable thinking/reasoning output (Gemini, DeepSeek)
    enable_prompt_caching: Optional[bool] = None  # Anthropic prompt caching (client default: True). Was a dead key in llm.yaml before this field existed.
    thinking_budget: Optional[int] = None  # Token budget for thinking (Gemini 2.5: 1-24576, default 8192)
    thinking_level: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]] = None  # Thinking level = OpenRouter reasoning.effort enum (none|minimal|low|medium|high|xhigh|max — "ultra" existed here but no provider knows it). Gemini 3 only knows minimal-high (higher values are clamped to high in the Gemini clients); "none" disables thinking on hybrid models (OpenRouter: DeepSeek V4 & co.)
    modalities: Optional[List[str]] = None  # Output modalities for audio models (e.g., ["text"] or ["text", "audio"])
    max_tokens: Optional[int] = None  # Maximum output tokens (limits response length, reduces costs)
    temperature: Optional[float] = None  # Sampling-Temperatur. None = Provider-Default (bei DeepSeek/OpenAI ~1,0!). Für mechanische Aufgaben (Struktur, Zuordnung, Extraktion) niedrig setzen: gemessen 2026-07-25 liefen alle Prosa-Konverter-Calls mit Provider-Default, also voller Sampling-Varianz für eine Kopier-Aufgabe. ⚠️ NICHT für Reasoning-Modelle setzen (gpt-5.x/o-Serie akzeptieren nur temperature=1 bzw. lehnen den Param ab) — dort steuert thinking_level. Wird als Top-Level-Feld an OpenAI-kompatible/Anthropic/Gemini-APIs gesendet.
    safety_settings: Optional[Dict[str, str]] = None  # Gemini safety settings: {HarmCategory: HarmBlockThreshold}
    service_tier: Optional[str] = None  # Service tier for OpenAI-compatible APIs (e.g. "flex" = Google Flex Processing via OpenRouter — cheaper, slower)
    prompt_cache_key: Optional[str] = None  # OpenAI Cache-Routing-Key (Chat + Responses API). AB GPT-5.6 PFLICHT fuer zuverlaessiges Prompt-Cache-Matching (Doku: "you must set prompt_cache_key ... for both implicit and explicit caching"); ohne Key cached 5.6 praktisch nie (belegt 2026-07-21: byte-identischer 10k-Prefix, cached_tokens=0). Empfohlener Wert "auto": Key wird pro Request gehasht aus fuehrendem System-Prompt (voll — ein langer System-Prompt darf das Fenster nicht auffressen) + erster Task-Message bis ~4096 Zeichen (llm/cache_key.py) — gleicher stabiler Prefix <-> gleicher Key, stabil ueber Folge-Turns, kollisionsfrei bei parallelen Buechern/Stories (statischer Agent-Name-Key wuerde dann Shard + ~15 req/min-Limit teilen). Statischer String weiterhin als explizites Override moeglich. Nur fuer OpenAI/OpenRouter-GPT-Modelle setzen — Fremd-Provider koennten den Param ablehnen.
    prompt_cache_mode: Optional[Literal["auto", "multi_turn", "task_sequence", "one_shot", "off"]] = None  # Cache-Verhalten des Agents (docs/prompt_cache_design.md §4). In Agent-yamls via llm_params "*" gesetzt (flache per-Agent-Form — es gibt keinen AgentConfig->Client-Pfad). "task_sequence" aktiviert die Segment-Leiter ueber deklarierten Sentinel-Grenzen; "multi_turn" dokumentiert Fortsetzungs-Caching (keine Marker); "off" strippt auch Sentinels. Default None ~ "auto" (nur deklarierte Grenzen markieren).
    prompt_cache_marker_style: Optional[Literal["openai", "anthropic", "none"]] = None  # Marker-Feld des Modells: "openai" = prompt_cache_breakpoint (GPT-5.6+), "anthropic" = cache_control ephemeral, "none" = Sentinels strippen. Default None = Client leitet ab (Anthropic-via-OpenRouter-Erkennung bzw. prompt_cache_key gesetzt -> openai, sonst none) — explizit setzen nur zum Uebersteuern.
    provider_routing: Optional[Dict[str, Any]] = None  # OpenRouter "provider" object: {order: [slugs], allow_fallbacks: bool, sort: "price", ...}. Order-only is enough to bias toward a sticky backend (improves implicit cache hit rate); allow_fallbacks: false would hard-pin. Systemweiter Default: llm_system.openrouter_routing (wird pro Schluessel von hier ueberstimmt). ⚠️ Nur die Pfade provider=openai_httpx und openai_responses reichen das Feld an den Request weiter — der SDK-Pfad provider=openai kennt es nicht und wuerde es STILL verwerfen (kein Modell im Katalog nutzt ihn; wer eines dorthin umstellt, verliert das Routing wortlos).
    reasoning_details_mode: Optional[Literal["keep_last", "keep_all", "strip"]] = None  # How to round-trip provider reasoning blocks across turns: "keep_last" (default, Gemini — current turn's thought signature only), "keep_all" (OpenAI reasoning models — encrypted chain must stay intact), "strip" (drop entirely). Literal: a typo must fail config load, not silently fall back to keep_last.

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
    # src/plugins_llm/ own the vocabulary via `provides_tts` in their
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


class LLMSystemConfig(BaseModel):
    """Complete LLM system configuration"""
    httpx_timeouts: Optional[HTTPXTimeoutConfig] = None  # Default HTTPX timeouts for all models
    openrouter_routing: Optional[Dict[str, Any]] = None  # System-Default fuer das OpenRouter-"provider"-Objekt (siehe LLMModelConfig.provider_routing). Gilt fuer JEDES Modell mit openrouter.ai-base_url; pro Schluessel gewinnt der Modell-Eintrag, ein bewusst gesetztes order/sort am Modell bleibt also stehen. {sort: price} waehlt den guenstigsten Anbieter (identisch zum Modell-Suffix ":floor"; erlaubt sind "price", "throughput", "latency"). ⚠️ sort schaltet OpenRouters Load-Balancing ab und verwaessert die Klebrigkeit der order-Eintraege, die hier den impliziten Prompt-Cache warm halten — ein Cache-Miss kostet bei Langkontext leicht mehr, als der guenstigere Anbieter spart. Alternative ohne diese Nebenwirkung: max_price als Deckel. Default None = aus.
    batch: Optional[BatchSystemConfig] = None  # Global batch processing configuration
    models: Dict[str, LLMModelConfig] = {}
    profiles: Dict[str, LLMProfile] = {}
    default_profile: Optional[str] = "normal"  # Default LLM profile to use

    @field_validator("profiles")
    @classmethod
    def _profile_names_must_not_shadow_model_fields(
        cls, v: Dict[str, "LLMProfile"]
    ) -> Dict[str, "LLMProfile"]:
        # Profilnamen sind Keys der profil-gekeyten agent_config.llm_params —
        # die Formerkennung (flat vs. gekeyt) unterscheidet Param-Namen von
        # Profilnamen. Ein Profil, das wie ein LLMModelConfig-Feld heisst
        # (z.B. "max_tokens"), waere dort unadressierbar → an der Wurzel
        # verbieten statt spaeter still fehlzuinterpretieren.
        shadowed = set(v) & set(LLMModelConfig.model_fields)
        if shadowed:
            raise ValueError(
                f"llm_system.profiles: Profilnamen {sorted(shadowed)} kollidieren "
                f"mit LLMModelConfig-Feldnamen (reserviert fuer llm_params) — "
                f"bitte umbenennen"
            )
        return v

    @model_validator(mode="after")
    def _profiles_must_point_at_usable_models(self) -> "LLMSystemConfig":
        """Eine Basisklasse ohne `model:` ist zum Erben da, nicht zum Fahren.

        Ohne diesen Riegel landet ihr None im Client und der Aufruf scheitert
        erst beim Provider — mit einer Meldung, die nichts mit der Config zu
        tun hat.
        """
        broken = sorted(
            f"{name} -> {profile.model_ref}"
            for name, profile in self.profiles.items()
            if profile.model_ref in self.models
            and not self.models[profile.model_ref].model
        )
        if broken:
            raise ValueError(
                "llm_system.profiles: diese Profile zeigen auf Eintraege ohne "
                f"`model:` — das sind Basisklassen zum Erben: {broken}")
        return self

    @model_validator(mode="after")
    def _providers_must_exist_as_plugins(self) -> "LLMSystemConfig":
        """A typo in `provider:` must fail at config load, not at first use.

        The provider vocabulary is owned by the plugins under
        src/plugins_llm/ (their manifests' `provides` lists) — this used to
        be a Literal here, which made the core the second registry. "batch"
        is the resolver-internal pseudo-provider and always allowed.

        Lenient on a missing/unreadable plugin root: config loading must not
        die because the scan failed — the registry raises its own, clearer
        error at build time in that case.
        """
        try:
            from agent_system.llm.registry import (
                batch_client_provider, known_batch_providers, known_providers,
                known_tts_providers)
            known = known_providers()
            known_tts = known_tts_providers()
        except Exception as e:
            # Lenient, but never silent: without this line an unreadable
            # plugin root turns the typo guard off with no trace, and every
            # agent dies individually at build time instead. Names BOTH
            # checks — the TTS half used to be skipped without mention.
            logging.getLogger(__name__).warning(
                "LLM and TTS provider validation skipped (manifest scan "
                "failed): %s", e)
            return self
        unknown = sorted(
            f"{name} (provider={m.provider})"
            for name, m in self.models.items()
            if m.provider != "batch" and m.provider not in known
        )
        if unknown:
            raise ValueError(
                f"llm_system.models: unknown provider on {unknown} — no plugin "
                f"under src/plugins_llm declares it (known: {sorted(known)})")

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
                    f"{unknown_batch_cfg} — no plugin under src/plugins_llm "
                    f"declares it via provides_batch "
                    f"(known: {sorted(known_batch_providers())})")

        unknown_tts = sorted(
            f"{name} (provider={m.provider})"
            for name, m in self.tts_models.items()
            if m.provider not in known_tts
        )
        if unknown_tts:
            raise ValueError(
                f"llm_system.tts_models: unknown TTS provider on {unknown_tts} "
                f"— no plugin under src/plugins_llm declares it via "
                f"provides_tts (known: {sorted(known_tts)})")
        return self

    # TTS (Text-to-Speech) configuration
    tts_models: Dict[str, TTSModelConfig] = {}
    tts_profiles: Dict[str, TTSProfile] = {}
    default_tts_profile: Optional[str] = None


# ===========================
# MCP Configuration Models
# ===========================

class ExternalServerConfig(BaseModel):
    """Configuration for external server connections and defaults"""
    # This matches the type comment in mcp.yaml for external_server
    pass  # Currently empty in YAML, can be extended


class ToolConfig(BaseModel):
    """Tool access control configuration"""
    allowed: Optional[List[str]] = Field(default_factory=list)  # list of allowed tools (use "*" to allow all tools)
    blocked: Optional[List[str]] = Field(default_factory=list)  # list of blocked tools

    def __init__(self, **data):
        # Normalize an explicit None to [] -- but only for keys that were
        # actually given. Injecting absent keys would mark them as "set",
        # so model_dump(exclude_unset=True) in the server-inheritance
        # resolver would export phantom empty lists that wipe parent lists.
        for key in ("allowed", "blocked"):
            if key in data and data[key] is None:
                data[key] = []
        super().__init__(**data)


class HooksConfig(BaseModel):
    """Hook system configuration for individual agents."""
    enabled: bool = True  # Global switch: enable/disable all hooks for this agent
    overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)  # Per-hook config (enabled, timeout, custom config, etc.)


class TimeoutConfig(BaseModel):
    """Timeout configuration for agent execution to prevent deadlocks."""
    # Queue handler timeouts
    status_queue_put_timeout: float = 5.0  # Timeout for status queue.put() operations (seconds)
    
    # LLM task polling timeout - safety net only!
    # For batch mode: The actual timeout is controlled by max_wait_hours in llm.yaml
    # For sync mode: The actual timeout is controlled by request_timeout in model config
    # This is just a safety net for truly stuck calls (default: 24 hours)
    llm_task_max_iterations: int = 864000  # 864000 * 0.1s = 24 hours safety net
    
    # Session lock timeout
    session_lock_timeout: float = 5.0  # Timeout for acquiring session lock (seconds)
    
    # Tool cleanup timeout
    tool_cleanup_timeout: float = 30.0  # Timeout for tool cleanup during cancellation (seconds)
    

class LoopDetectionConfig(BaseModel):
    """Configuration for tool call loop detection.
    
    Prevents agents from getting stuck in infinite loops calling
    the same tool(s) repeatedly with identical arguments.
    """
    enabled: bool = True  # Enable/disable loop detection
    history_size: int = 20  # Number of recent tool calls to track
    exact_match_threshold: int = 3  # Warn after N identical calls
    sequence_threshold: int = 2  # Warn after N repeated sequences
    block_after_threshold: int = 5  # Block tool after N repetitions
    auto_unblock_after_steps: int = 3  # Unblock tools after N steps


# Nicht per agent_config.llm_params ueberschreibbar: diese Felder definieren
# die IDENTITAET des Modells (dafuer gibt es llm_profile / llm.yaml).
LLM_PARAMS_PROTECTED_FIELDS = frozenset({
    "provider", "model", "api_key", "base_url", "batch_provider", "ollama_mode",
})


def resolve_llm_params(
    params: Optional[Dict[str, Any]], profile: str
) -> Optional[Dict[str, Any]]:
    """Effektive flache LLM-Params fuer EIN Profil.

    Flat-Form ({param: wert}) gilt unveraendert fuer jedes Profil, auf das
    der Aufrufer sie anwendet. Profil-gekeyte Form ({profil: {param: wert}})
    loest zu merge("*", params[profil]) auf — der spezifische Eintrag
    gewinnt. Erkennung ist eindeutig: Flat-Keys sind LLMModelConfig-
    Feldnamen, Profil-Keys nicht (Mischformen lehnt der AgentConfig-
    Validator beim Config-Load ab).
    """
    if not params:
        return None
    if any(k in LLMModelConfig.model_fields for k in params):
        return params  # Flat-Form
    merged = {**(params.get("*") or {}), **(params.get(profile) or {})}
    return merged or None


# Fallback-Profile nutzen dieselbe resolve_llm_params-Semantik wie die
# Primaermodelle: Flat-Form und "*" gelten fuer ALLE Ketten-Mitglieder,
# der exakt gekeyte Eintrag gewinnt. Wer "*" setzt, entscheidet das
# bewusst fuer die ganze Kette — Cross-Provider-Vertraeglichkeit der
# Werte liegt beim Operator (profil-gekeyte Eintraege erlauben die
# Feinsteuerung pro Modell).


class SkillsConfig(BaseModel):
    """Which packaged skills an agent gets (see docs/skills_design.md).

    ``always`` skills are appended to the system prompt at render time — the
    agent HAS the knowledge, it does not have to ask for it. Deterministic and
    cache-friendly (part of the stable prefix).

    Accepts a bare list as shorthand, so both forms work::

        skills: ["house-style"]              # == always: ["house-style"]
        skills:
          always: ["house-style"]

    ``on_demand`` skills are NOT put in the prompt — only a one-line index of
    them is, so the agent knows they exist and can pull the body (and any
    bundled ``reference/`` files) with the ``skills`` plugin's tools. Use it for
    material that is large and only occasionally needed; anything the agent
    needs most of the time belongs in ``always``, where it cannot be forgotten.
    """

    always: List[str] = Field(default_factory=list)
    on_demand: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_list(cls, v: Any) -> Any:
        if isinstance(v, (list, tuple)):
            return {"always": list(v)}
        return v


class AgentConfig(BaseModel):
    """Configuration for individual agent instances (matches type comment in mcp.yaml)"""

    # extra="forbid", not the pydantic default "ignore". A keyword this model
    # does not know used to be dropped without a word — and
    # `default_llm_profile` is a read-only PROPERTY below, so
    # `AgentConfig(default_llm_profile="turbo")` silently kept the "normal"
    # default. Two plugins built their LLM that way and ran for months on a
    # model nobody chose. A typo in a config key has to be loud; it is the
    # cheapest possible moment to catch it.
    #
    # Measured before switching: zero agent_config blocks in config/ or src/
    # carry an unknown key, so no deployment is refused by this.
    model_config = ConfigDict(extra="forbid")
    # LLM-KETTE (seit 2026-07: neue Semantik!): Liste = [primär, fallback1, fallback2, ...]
    # — Position 0 ist das Standard-Modell, ALLE weiteren Einträge sind Fallbacks
    # in Reihenfolge (Rate-Limit/Upstream-Fehler). String = nur Primär, keine Fallbacks.
    llm_profile: str | List[str] = "normal"
    # Advanced-KETTE (use_advanced_model=True): gleiche Struktur — [primär_adv,
    # fallback1_adv, ...]. Leer/fehlend = KEIN Advanced-Modell (use_advanced_model
    # läuft dann auf der normalen Kette weiter). Die Ketten sind FÜREINANDER das
    # letzte Sicherheitsnetz: ist die eigene Kette bei Fallbacks erschöpft, wird
    # die jeweils andere Kette komplett durchprobiert (fallback_chain()).
    llm_profile_advanced: Optional[List[str]] = None
    # ENTFERNT (alte Semantik [std_fallback, adv_fallback]) — Migration:
    # scripts/migrate_llm_profiles.py. Absichtlich als Feld behalten, damit
    # unmigrierte yamls LAUT beim Laden scheitern statt still falsch zu laufen.
    llm_profile_fallbacks: Optional[List[str]] = None
    # Per-Agent LLM-Parameter-Overrides: werden beim Aufloesen der llm_profile-
    # Modelle (default/advanced/escalation) ueber den referenzierten
    # llm_system.models-Eintrag gelegt — statt fuer jede Kombination
    # (Modell x thinking_level x max_tokens ...) einen eigenen Model-Eintrag
    # anzulegen. Erlaubt sind alle LLMModelConfig-Felder AUSSER den
    # Identitaets-Feldern (provider/model/api_key/base_url/batch_provider/
    # ollama_mode — die definieren WELCHES Modell und gehoeren in llm.yaml).
    # Gilt einheitlich fuer ALLE Ketten-Mitglieder (Primaer + Fallbacks
    # beider Ketten, Eskalation): Flat-Form und "*" wirken ueberall, der
    # exakt gekeyte Eintrag gewinnt. Cross-Provider-Vertraeglichkeit
    # pauschaler Werte ("*" mit thinking_level=max auf einem Gemini-
    # Fallback) verantwortet der Operator — profil-gekeyte Eintraege
    # erlauben die Feinsteuerung pro Modell. Explizite --llm-profile-
    # Overrides laufen weiterhin ohne llm_params.
    #
    # ZWEI Formen (unterschiedliche Modelle kennen unterschiedliche Keys):
    #   flat  — gilt fuer die ganze Kette (wie "*"):
    #     llm_params: { max_tokens: 8000 }
    #   profil-gekeyt — Params kleben am Modell, nicht am Slot;
    #     "*" gilt fuer alle Ketten-Mitglieder, spezifischer Eintrag gewinnt:
    #     llm_params:
    #       "*": { max_tokens: 8000 }
    #       or-gpt-full-unlimited: { thinking_level: high }
    #   Unbekannte Keys (in keiner Kette) sind ungueltig. Mischformen ebenso.
    llm_params: Optional[Dict[str, Any]] = None
    fallback_recovery_seconds: int = 3600  # Seconds before trying original LLM again after rate limit (default: 1 hour)
    fallback_recovery_jitter_percent: float = 20.0  # Random jitter ±X% to prevent thundering herd when multiple agents recover
    max_steps: int = 20  # maximum steps for agents that support multi-step reasoning (default: 20, used if not set in config)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    hooks: Optional[HooksConfig] = None  # Hook system configuration (optional)
    system_template: Optional[str] = None  # Path to system prompt template file
    system_prompt: Optional[str] = None  # Inline system prompt (alternative to system_template)
    # Packaged knowledge appended to the system prompt (docs/skills_design.md).
    # Bare list allowed: `skills: ["house-style"]`.
    skills: Optional[SkillsConfig] = None
    template_vars: Optional[Dict[str, Any]] = None  # Custom variables for Jinja2 template rendering
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)  # Timeout configuration for deadlock prevention
    loop_detection: LoopDetectionConfig = Field(default_factory=LoopDetectionConfig)  # Tool call loop detection
    # Auto-escalate to the advanced model (llm_profile_advanced[0]) when the run
    # loop observes the agent is stuck (loop detector, or repeated all-error tool
    # steps). Time-boxed + budget-capped; needs a non-empty llm_profile_advanced
    # (distinct from the default). No-op when already running advanced.
    auto_escalate_on_stuck: bool = False
    escalate_rounds: int = 2          # steps to stay on the advanced model per trigger
    escalate_max_calls: int = 6       # total advanced calls allowed per run (budget)
    escalate_error_streak: int = 2    # trigger after N consecutive all-error tool steps

    @field_validator("system_template")
    @classmethod
    def _validate_system_template_ext(cls, v: Optional[str]) -> Optional[str]:
        """Prompt templates are markdown only (the multi-section YAML format was
        removed). Reject a non-markdown extension at CONFIG LOAD with a migration
        hint, so a stale/typo'd path fails fast at startup instead of raising
        mid-run on the first (and every) render step."""
        if v and not v.lower().endswith((".md", ".txt", ".markdown")):
            raise ValueError(
                f"system_template '{v}' must be a markdown file (.md/.txt/"
                f".markdown). YAML prompt templates are no longer supported — "
                f"convert it to a single markdown file (Jinja2 variables still "
                f"work).")
        return v

    @field_validator("llm_params")
    @classmethod
    def _validate_llm_params(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not v:
            return v or None
        model_fields = set(LLMModelConfig.model_fields.keys())
        allowed = model_fields - LLM_PARAMS_PROTECTED_FIELDS

        def _check_flat(params: Dict[str, Any], where: str) -> None:
            protected = set(params) & LLM_PARAMS_PROTECTED_FIELDS
            if protected:
                raise ValueError(
                    f"llm_params{where}: Identitaets-Felder {sorted(protected)} sind "
                    f"gesperrt (nicht erlaubte Keys — dafuer gibt es llm_profile/llm.yaml)"
                )
            unknown = set(params) - allowed
            if unknown:
                raise ValueError(
                    f"llm_params{where}: nicht erlaubte Keys {sorted(unknown)} — "
                    f"erlaubt sind: {sorted(allowed)}"
                )
            # Typ-/Wert-Validierung gegen das echte Modell-Schema (fail fast
            # beim Config-Load statt erst beim ersten LLM-Call).
            LLMModelConfig.model_validate({"model": "_llm_params_probe_", **params})

        # Form-Erkennung: Param-Namen (LLMModelConfig-Felder) = Flat-Eintrag,
        # alles andere ("*", Profilnamen) = Profil-Key. Mischformen ungueltig.
        flat_keys = [k for k in v if k in model_fields]
        profile_keys = [k for k in v if k not in model_fields]
        if flat_keys and profile_keys:
            # Skalare Werte unter Nicht-Feld-Keys koennen keine Profil-
            # Eintraege sein → das ist ein Tippfehler in flat-Params, keine
            # Mischform. Praezise Meldung mit erlaubten Keys statt Form-Rüge.
            typo_keys = [k for k in profile_keys if not isinstance(v[k], dict)]
            if typo_keys:
                raise ValueError(
                    f"llm_params: nicht erlaubte Keys {sorted(typo_keys)} — "
                    f"erlaubt sind: {sorted(allowed)}"
                )
            raise ValueError(
                f"llm_params: Mischform aus Params {sorted(flat_keys)} und "
                f"Profil-Keys {sorted(profile_keys)} — entweder flat "
                f"({{param: wert}}) ODER profil-gekeyt ({{profil: {{param: wert}}}}). "
                f"Entsteht auch durch Typ-Vererbung (Parent flat + Kind gekeyt): "
                f"dann im Parent die flat-Params semantikgleich unter '*' legen."
            )
        if profile_keys:
            for pk, sub in v.items():
                if not isinstance(sub, dict):
                    raise ValueError(
                        f"llm_params: nicht erlaubte Keys ['{pk}'] — weder "
                        f"LLM-Param (erlaubt: {sorted(allowed)}) noch "
                        f"Profil-Key mit Param-Dict als Wert"
                    )
                _check_flat(sub, f"['{pk}']")
            return v
        _check_flat(v, "")
        return v

    @model_validator(mode="after")
    def _reject_empty_llm_chain(self) -> "AgentConfig":
        # `llm_profile: []` is not "use the default" — it is a chain the
        # operator wrote and that resolves to nothing. default_llm_profile
        # then hands out "normal", a profile nobody configured here, and the
        # error arrives later as "Profile 'normal' not found" pointing at a
        # name that appears nowhere in this file.
        if isinstance(self.llm_profile, list) and not self.llm_profile:
            raise ValueError(
                "llm_profile: [] is not a chain — name a profile "
                "([primary, fallback...]) or drop the field so the default "
                "applies.")
        return self

    @model_validator(mode="after")
    def _reject_legacy_fallbacks(self) -> "AgentConfig":
        # Alte [std, adv]-Positions-Semantik ist entfernt. Ein gesetztes
        # llm_profile_fallbacks bedeutet: yaml wurde nicht migriert — laut
        # scheitern statt still falsch laufen (llm_profile[1] wäre sonst
        # plötzlich Fallback statt Advanced).
        if self.llm_profile_fallbacks:
            raise ValueError(
                "llm_profile_fallbacks wurde entfernt. Neue Semantik: "
                "llm_profile = [primär, fallback1, ...] (Kette) und "
                "llm_profile_advanced = [primär_adv, fallback1_adv, ...]. "
                "Migration: python scripts/migrate_llm_profiles.py"
            )
        return self

    @model_validator(mode="after")
    def _validate_llm_params_profile_keys(self, info: ValidationInfo) -> "AgentConfig":
        # Profil-gekeyte llm_params: gueltige Keys sind "*" plus ALLE
        # Mitglieder beider Ketten (Primaer + Fallbacks) — die Params wirken
        # einheitlich auf jedes Ketten-Mitglied ("*"/flat ueberall, exakter
        # Eintrag gewinnt). Unbekannte Keys (Tippfehler, verwaiste Eintraege
        # nach Ketten-Umbau) waeren stille No-Ops und sollen beim
        # Config-Load knallen.
        p = self.llm_params
        if p and not any(k in LLMModelConfig.model_fields for k in p):
            valid = {"*"}
            chain = self.llm_profile if isinstance(self.llm_profile, list) else [self.llm_profile]
            valid.update(chain)
            valid.update(self.llm_profile_advanced or [])
            unknown = set(p) - valid
            if unknown:
                msg = (
                    f"llm_params: profile keys {sorted(unknown)} appear in no "
                    f"LLM chain of this agent — valid: {sorted(valid)}. "
                    f"Also caused by type inheritance when the child overrides "
                    f"the parent's chains: move the keyed llm_params in the "
                    f"parent to '*', or move them into the child together "
                    f"with the chains."
                )
                # The caller decides whether a stale key costs the start. The
                # real config load drops it and says so loudly instead — one
                # key in ONE agent used to keep every server down. Everywhere
                # else it stays an error: same rule, different consequence.
                if not (info.context or {}).get("drop_stale_llm_params"):
                    raise ValueError(msg)
                for key in unknown:
                    p.pop(key, None)
        return self

    @property
    def default_llm_profile(self) -> str:
        """Primäres LLM-Profil (Position 0 der Kette bzw. der String)."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile[0] if self.llm_profile else "normal"
        return self.llm_profile

    @property
    def available_llm_profiles(self) -> List[str]:
        """ALLE dem Agent zugeordneten Profile (normale + Advanced-Kette,
        dedupliziert, Reihenfolge stabil) — für Auswahl-Enums (schema_based)
        und Validierung expliziter llm_profile-Parameter."""
        base = list(self.llm_profile) if isinstance(self.llm_profile, list) else [self.llm_profile]
        seen: list[str] = []
        for p in base + (self.llm_profile_advanced or []):
            if p not in seen:
                seen.append(p)
        return seen

    @property
    def advanced_llm_profile(self) -> Optional[str]:
        """Primäres Advanced-Profil (use_advanced_model=True) — None wenn
        keine Advanced-Kette konfiguriert ist (dann läuft advanced = normal)."""
        adv = self.llm_profile_advanced or []
        return adv[0] if adv else None

    @property
    def fallback_profiles(self) -> List[str]:
        """Fallback-Kette des NORMALEN Modus: llm_profile[1:]."""
        if isinstance(self.llm_profile, list):
            return self.llm_profile[1:]
        return []

    def fallback_chain(self, use_advanced_model: bool = False,
                       exclude: Optional[str] = None) -> List[str]:
        """Fallback-Reihenfolge für den Retry-Loop (ohne das aktive Primär-Modell).

        Die Ketten sind FÜREINANDER das letzte Sicherheitsnetz (symmetrisch):
        - normal:   llm_profile[1:]           + komplette Advanced-Kette
        - advanced: llm_profile_advanced[1:]  + komplette normale Kette
        Erhält die alte Resilienz („Ultimate-Fallback wenn beide Provider down"):
        ein Lauf fällt nie ins Leere, solange IRGENDEINE Kette noch ein Modell
        hat. Dedupliziert, Reihenfolge stabil, aktives Primär-Modell exkludiert.

        exclude: Profil-Name des TATSÄCHLICH aktiven Modells, wenn es vom
        Config-Primär abweicht (Eskalations-Swap, explizites llm_profile-
        Override) — sonst würde das gerade fehlschlagende Modell als sein
        eigener Fallback erneut versucht (Doppel-Retry).
        """
        base = list(self.llm_profile) if isinstance(self.llm_profile, list) else [self.llm_profile]
        adv = list(self.llm_profile_advanced or [])
        if use_advanced_model and self.advanced_llm_profile:
            chain = adv[1:] + base
            active = self.advanced_llm_profile
        else:
            chain = base[1:] + adv
            active = self.default_llm_profile
        seen: list[str] = []
        for p in chain:
            if p not in seen and p != active and p != exclude:
                seen.append(p)
        return seen


class AgentMetadata(BaseModel):
    """Metadata for agent configuration.

    Provides additional information about agents for discoverability,
    categorization, and visibility control.
    """
    author: Optional[str] = None  # Author/creator of the agent
    version: Optional[str] = None  # Version string (e.g., "1.0.0")
    tags: Optional[List[str]] = None  # Tags for categorization/search
    category: Optional[str] = None  # Category (e.g., "financial", "research", "development")

    # Visibility control: determines where the agent appears
    # Default: "private" - agents must explicitly opt-in to visibility
    visibility: Literal["ui", "tool", "both", "private"] = "private"
    # - "ui": Visible in UI dropdown, NOT available as tool for other agents
    # - "tool": Available as tool for other agents, NOT in UI dropdown
    # - "both": Visible in UI AND available as tool
    # - "private": Neither UI nor tool (for testing/experimental agents)


class MCPConfig(BaseModel):
    """MCP configuration (matches type comment in mcp.yaml for default_config)"""
    model_config = {"extra": "allow"}  # Allow extra fields for plugin-specific config

    type: str = "basic_agent"   # type of mcp-server/agent to use
    enabled: bool = False       # enable or disable this mcp-server/agent
    description: Optional[str] = None  # Human-readable description of this instance
    self_tool_descriptions: Optional[Dict[str, str]] = Field(default_factory=dict)  # Custom descriptions for this server's own tools
    agent_config: Optional[AgentConfig] = None
    metadata: Optional[AgentMetadata] = None  # Instance metadata (author, version, visibility)


class ExternalServerConnectionConfig(BaseModel):
    """Configuration for external server connections"""
    timeout: float = 5.0
    parallel_connect: bool = True


class ExternalServerCacheConfig(BaseModel):
    """Configuration for external server caching"""
    enabled: bool = True  # Enable tool list caching
    tool_list_ttl: float = 30.0  # TTL for the mcp_client plugin's tool-list cache
    max_size: Optional[int] = None  # Maximum cache entries (None = unlimited)


class MCPAuthConfig(BaseModel):
    """Authentication configuration for MCP servers"""
    type: str = "none"  # none, bearer, api_key, basic
    api_key: Optional[str] = None
    api_key_header: str = "Authorization"
    bearer_token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    # Retry settings
    max_retries: int = 3
    retry_delay: float = 1.0


class RemoteMCPConfig(BaseModel):
    """Configuration for a remote MCP server"""
    url: str = ""
    enabled: bool = False
    description: Optional[str] = None
    transport: str = "streaming"
    initialization_options: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, bool]] = None
    tools: Optional[ToolConfig] = None

    # Local servers (transport: stdio) are launched instead of dialled, so they
    # need a command rather than a url. Without these fields pydantic dropped
    # them silently (extra="ignore") and the stdio transport was unreachable
    # from a real config, however correctly it was written.
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None

    # Authentication and security
    auth: Optional[MCPAuthConfig] = None



class ExternalServersConfig(BaseModel):
    """Configuration for all external servers"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


class SkillsSystemConfig(BaseModel):
    """Where packaged skills are discovered (matches the ``skills:`` block).

    Mirrors ``plugins.plugin_dirs``: several roots, each of whose immediate
    subdirectories may be a skill. Relative paths resolve like plugin dirs
    (config-folder first, then repo root). The first root defining a name wins.

    Empty means "use the defaults" (``skills/`` and ``.claude/skills/``, or
    ``$AGENT_SKILL_DIRS``), so an existing config without this block keeps
    working. Note the inverse: a config that DOES list roots replaces the
    defaults entirely — listing only ``skills`` hides ``.claude/skills``.
    """

    skill_dirs: List[str] = Field(default_factory=list)


class PluginsConfig(BaseModel):
    """Configuration for local plugins (matches config/plugins.yaml)"""
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: MCPConfig = Field(default_factory=MCPConfig)
    servers: Dict[str, MCPConfig] = Field(default_factory=dict)  # Named MCP server configurations


class MCPServersConfig(BaseModel):
    """Configuration for external MCP servers (matches config/mcp_servers.yaml)"""
    connection: ExternalServerConnectionConfig = Field(default_factory=ExternalServerConnectionConfig)
    cache: ExternalServerCacheConfig = Field(default_factory=ExternalServerCacheConfig)
    remote_servers: Dict[str, RemoteMCPConfig] = Field(default_factory=dict)


# Backward compatibility: Keep MCPSystemConfig for transition period
class MCPSystemConfig(BaseModel):
    """
    DEPRECATED: Old monolithic MCP system configuration.
    Use separate configs instead: PluginsConfig, MCPServersConfig.
    This model is kept for backward compatibility during migration.
    """
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: MCPConfig = Field(default_factory=MCPConfig)
    external_servers: ExternalServersConfig = Field(default_factory=ExternalServersConfig)
    servers: Dict[str, MCPConfig] = Field(default_factory=dict)  # Named MCP server configurations


# ===========================
# Core Configuration Models
# ===========================

class NetworkConfig(BaseModel):
    """Network configuration for the application"""
    ssl_verify: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    disable_cache: bool = True
    
    # HTTP connection pooling settings
    http_connection_limit: int = 10  # Total HTTP connections for MCP streamable transport
    http_connection_limit_per_host: int = 5  # HTTP connections per host
    
    # CLI request timeout
    cli_request_timeout: float = 5.0  # Timeout for CLI HTTP requests (seconds)


class CancellationConfig(BaseModel):
    """Configuration for the cancellation system"""
    cleanup_timeout: float = 10.0  # Seconds to wait for graceful cleanup before forcing termination
    monitor_interval: float = 1.0  # Seconds between timeout checks


class LoggingConfig(BaseModel):
    """Logging configuration"""
    enabled: bool = True
    level: str = "DEBUG"
    file: str = "logs/agent.log"
    file_cli: Optional[str] = None
    file_api: Optional[str] = None
    cancellation: Optional[CancellationConfig] = None
    
    # Log rotation settings
    rotation_enabled: bool = True  # Enable log rotation
    max_bytes: Union[int, str] = Field(default="10MB")  # Max size per log file (int in bytes or string like "10MB", "100KB", "1GB")
    backup_count: int = 5  # Number of backup files to keep
    
    @field_validator('max_bytes', mode='before')
    @classmethod
    def parse_max_bytes(cls, v: Union[int, str]) -> int:
        """Parse max_bytes from human-readable format (e.g., '10MB') to bytes."""
        if isinstance(v, int):
            return v
        
        if isinstance(v, str):
            # Match number followed by optional unit (KB, MB, GB, case-insensitive)
            match = re.match(r'^(\d+(?:\.\d+)?)\s*(KB|MB|GB|K|M|G)?$', v.strip(), re.IGNORECASE)
            if not match:
                raise ValueError(
                    f"Invalid size format: '{v}'. "
                    "Expected format: number with optional unit (KB/MB/GB), e.g., '10MB', '100KB', '1GB'"
                )
            
            number = float(match.group(1))
            unit = (match.group(2) or '').upper()
            
            # Convert to bytes
            multipliers = {
                '': 1,
                'K': 1024,
                'KB': 1024,
                'M': 1024 * 1024,
                'MB': 1024 * 1024,
                'G': 1024 * 1024 * 1024,
                'GB': 1024 * 1024 * 1024,
            }
            
            return int(number * multipliers[unit])
        
        raise ValueError(f"max_bytes must be int or string, got {type(v).__name__}")
    
    def model_post_init(self, __context) -> None:
        """Post-init hook to parse default values through validator."""
        # Manually trigger validation for max_bytes if it's still a string
        if isinstance(self.max_bytes, str):
            self.max_bytes = self.parse_max_bytes(self.max_bytes)


class ContextConfig(BaseModel):
    """Context information configuration"""
    auto_datetime: bool = True
    timezone: str = "Europe/Berlin"
    location: str = "Germany"


class StatusConfig(BaseModel):
    """Status message system configuration"""
    queue_maxsize: int = 1000  # Max events per queue (prevents memory exhaustion)
    drop_oldest_when_full: bool = True  # Drop oldest events when queue is full
    
    # SSE connection keep-alive settings
    sse_keepalive_interval: float = 15.0  # Interval for SSE keep-alive comments (seconds)
    llm_heartbeat_interval: float = 5.0  # Interval for heartbeat events during LLM calls (seconds)


class VisionConfig(BaseModel):
    """Vision/image processing configuration"""
    image_warn_size_mb: float = 10.0  # Warn when images exceed this size (MB)
    image_max_size_mb: Optional[float] = None  # Maximum allowed image size (MB), None = no limit


# ===========================
# Security Configuration Models
# ===========================

class AnonymousAccessConfig(BaseModel):
    """Configuration for anonymous (unauthenticated) access.
    
    When auth.enabled=true but anonymous_access.enabled=true,
    unauthenticated users can access certain endpoints with
    a limited guest role.
    """
    enabled: bool = False  # Allow unauthenticated access to certain endpoints
    role: str = "guest"  # Role assigned to anonymous users
    allowed_endpoints: List[str] = Field(default_factory=lambda: [
        "GET /health",
        "GET /static/*",
        "GET /login",
        "POST /auth/login",
        "POST /auth/register",  # Allow self-registration when enabled
    ])
    rate_limit_multiplier: float = 0.5  # Stricter rate limit for anonymous (50% of normal)


class EndpointSecurityRule(BaseModel):
    """Security rule for an endpoint pattern.
    
    Patterns support:
    - Exact match: "GET /agents"
    - Wildcard: "/admin/*", "* /sessions/*"
    - Method prefix: "POST /run", "GET /events"
    """
    pattern: str  # e.g., "POST /run", "/admin/*", "GET /sessions/*"
    policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    min_role: Optional[str] = None  # "admin", "user", "guest"
    description: Optional[str] = None  # Human-readable description


class EndpointSecurityConfig(BaseModel):
    """Endpoint-level security configuration.
    
    When auth.enabled=true, this config controls which endpoints
    require authentication and what role is needed.
    """
    # Default policy when no specific rule matches
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    
    # Audit logging for all endpoint access (logs to security.log)
    audit_enabled: bool = True
    
    # Custom rules (processed in order, first match wins)
    rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Admin endpoints always require admin role
        EndpointSecurityRule(
            pattern="/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Admin endpoints require admin role"
        ),
        # Agent execution requires user role
        EndpointSecurityRule(
            pattern="POST /run",
            policy="require_auth",
            min_role="user",
            description="Agent execution requires authentication"
        ),
        # Events stream requires user role
        EndpointSecurityRule(
            pattern="GET /events",
            policy="require_auth",
            min_role="user",
            description="Event stream requires authentication"
        ),
        # Session management requires user role
        EndpointSecurityRule(
            pattern="* /sessions/*",
            policy="require_auth",
            min_role="user",
            description="Session management requires authentication"
        ),
    ])


class LLMSecurityConfig(BaseModel):
    """Security configuration for LLM API calls.
    
    These settings ensure that LLM requests (which cost money)
    are properly authorized and audited.
    """
    require_valid_user: bool = True  # LLM calls require authenticated user context
    validate_session_ownership: bool = True  # Users can only access their own sessions
    audit_llm_requests: bool = True  # Log all LLM requests with user info
    max_tokens_per_request_anonymous: Optional[int] = None  # Token limit for anonymous (None = blocked)
    max_requests_per_hour_anonymous: int = 0  # Hourly limit for anonymous users (0 = blocked)


class PluginSecurityConfig(BaseModel):
    """Security configuration for plugin web endpoints.
    
    Controls how plugin-provided HTTP endpoints are secured.
    By default, all plugin endpoints require authentication.
    """
    # Global default for all plugin endpoints
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    default_min_role: str = "user"  # Default minimum role for plugin endpoints
    
    # Plugin-specific overrides (plugin_name -> config)
    # Example: {"todo": {"policy": "allow_anonymous"}, "admin_tools": {"min_role": "admin"}}
    plugin_overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    # Endpoint pattern overrides (same format as endpoint_security.rules)
    # These take precedence over plugin_overrides
    endpoint_rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Example: Block all plugin admin endpoints for non-admins
        EndpointSecurityRule(
            pattern="/plugins/*/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Plugin admin endpoints require admin role"
        ),
    ])


class AuthConfig(BaseModel):
    """Authentication and authorization configuration.
    
    Multi-layered security configuration for the AgentSystem.
    
    Security Layers:
    1. Transport: HTTPS (handled externally by reverse proxy)
    2. Rate Limiting: Per-IP request limiting
    3. Authentication: JWT tokens / API keys
    4. Authorization: Role-based access control
    5. Request Context: User context in agent execution
    """
    enabled: bool = False  # Master switch for authentication system
    secret_key: str = "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30  # Refresh token valid for 30 days

    # Database settings
    database_path: str = "data/users.db"

    # Security settings
    rate_limit_enabled: bool = True
    requests_per_minute: int = 60
    security_headers_enabled: bool = True

    # CORS settings
    # cors_credentials defaults to False because cors_origins defaults to the
    # wildcard, and the two are mutually exclusive (configure_cors would drop
    # credentials and warn). Enable it together with an explicit origin
    # allowlist for a cross-origin frontend; the bundled same-origin UI never
    # needs it.
    cors_enabled: bool = True
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    cors_credentials: bool = False
    cors_methods: List[str] = Field(default_factory=lambda: ["*"])
    cors_headers: List[str] = Field(default_factory=lambda: ["*"])

    # Trusted hosts (optional)
    trusted_hosts: Optional[List[str]] = None

    # Default admin user (created on first startup if no users exist)
    default_admin_username: str = "admin"
    default_admin_password: Optional[str] = None  # Generated randomly if not set
    default_admin_email: str = "admin@localhost"
    
    # NEW: Anonymous access configuration
    anonymous_access: AnonymousAccessConfig = Field(default_factory=AnonymousAccessConfig)
    
    # NEW: Endpoint-level security rules
    endpoint_security: EndpointSecurityConfig = Field(default_factory=EndpointSecurityConfig)
    
    # NEW: LLM request security
    llm_security: LLMSecurityConfig = Field(default_factory=LLMSecurityConfig)
    
    # NEW: Plugin endpoint security
    plugin_security: PluginSecurityConfig = Field(default_factory=PluginSecurityConfig)


class AgentSystemConfig(BaseModel):
    """Main configuration model for the entire AgentSystem"""
    # Basic metadata
    name: str = "AgentSystem"
    version: str = "0.0.0"
    description: str = "Scarab Flexible AI Agent System using MCP"

    # Include references (for documentation purposes)
    includes: Optional[List[str]] = None

    # Core configurations
    context: ContextConfig = Field(default_factory=ContextConfig)
    status: StatusConfig = Field(default_factory=StatusConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    default_agent: str = "basic_agent"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    # Skill discovery roots (docs/skills_design.md). Per-agent selection is
    # AgentConfig.skills; this is only WHERE skills are found.
    skills: SkillsSystemConfig = Field(default_factory=SkillsSystemConfig)

    # Included configurations (will be populated from included files)
    llm_system: Optional[LLMSystemConfig] = None

    # New structure (Epic 0044) - matches YAML keys
    plugins: Optional[PluginsConfig] = None  # From config/plugins.yaml -> plugins:
    external_servers: Optional[MCPServersConfig] = None  # From config/mcp_servers.yaml -> external_servers:
