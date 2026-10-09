"""An agent's configuration -- ``agent_config`` with its tools, hooks, timeouts, loop detection,
skills and LLM chains, and the metadata beside it -- and the ``plugins:`` section, whose server
entries (ToolServerConfig) carry both.

The per-agent ``llm_params`` are here as well (LLM_PARAMS_PROTECTED_FIELDS, resolve_llm_params):
they are LLMModelConfig fields, but which of them an agent may set and how a profile-keyed block
resolves is the agent's side of that contract, checked by AgentConfig.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, ValidationInfo, model_validator
from typing import Literal, Optional, Dict, List, Any

from .llm import LLMModelConfig


# ===========================
# Tool server configuration Models
# ===========================

class ToolConfig(BaseModel):
    """Tool access control configuration"""
    allowed: Optional[List[str]] = Field(default_factory=list)  # list of allowed tools (use "*" to allow all tools)
    blocked: Optional[List[str]] = Field(default_factory=list)  # list of blocked tools
    # Allowed tools whose schema is held back until the model loads it with
    # tool_search (servers/agent/deferred_tools.py). Read for agents only; an
    # external MCP server entry (RemoteMCPConfig.tools) ignores it.
    deferred: Optional[List[str]] = Field(default_factory=list)

    def __init__(self, **data):
        # Normalize an explicit None to [] -- but only for keys that were
        # actually given. Injecting absent keys would mark them as "set",
        # so model_dump(exclude_unset=True) in the server-inheritance
        # resolver would export phantom empty lists that wipe parent lists.
        for key in ("allowed", "blocked", "deferred"):
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


class ReasoningLoopConfig(BaseModel):
    """Configuration for reasoning loop detection.

    Catches a model that is stuck inside its own THINKING and keeps paying
    for it — measured: one run spent 65.535 reasoning tokens over 20 minutes
    repeating a single sentence. The run loop aborts such a call and retries
    the same model once.

    Only the two knobs worth turning live here. The measurement geometry
    (window size, check interval, compared piece length) stays in
    ``servers/agent/reasoning_loop.py``: those three were calibrated TOGETHER
    with the threshold, so changing one here would quietly make the threshold
    mean something else.
    """
    enabled: bool = True  # Enable/disable reasoning loop detection
    # Measured over 4.916 stored runs: the worst healthy window scored 0.19,
    # the two stuck runs 0.90 and 0.95. Anything in 0.30..0.60 caught both
    # with zero false alarms, so this sits in the middle of an empty gap.
    repetition_threshold: float = 0.5


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
    """Which packaged skills an agent gets.

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


def _merges_into_inherited(entries: Any) -> bool:
    """Whether a list merges into the inherited one (``+``/``!`` entries) instead of replacing it."""
    return isinstance(entries, list) and any(str(e)[:1] in ("+", "!") for e in entries)


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
    # Run on the caller's LLM (opt-in): when the run that starts this agent was
    # switched to another profile than its agent's own (API llm_profile, the
    # web chat's model picker, CLI --llm, the chat's /model, use_advanced_model
    # -- or it followed its own caller this way), this agent runs on that
    # profile too, with its own llm_params for it; its chain stays the
    # fallback, its own primary first. Without such a switch it runs its own
    # chain. A choice made for this very run wins: an override
    # passed to it, or use_advanced_model. Any plugin that starts sub-agents
    # inside a tool call gets this without doing anything (llm/caller_llm.py);
    # a run started later in another process does not.
    inherit_parent_llm: bool = False
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
    # Longest block this agent puts on an LLM (llm/model_health.py, for every agent):
    # a rate limit starts at 60 s and doubles up to this; an exhausted quota or a
    # refused key (401/402/403/404) blocks this long at once.
    fallback_recovery_seconds: int = 3600
    max_steps: int = 20  # maximum steps for agents that support multi-step reasoning (default: 20, used if not set in config)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    hooks: Optional[HooksConfig] = None  # Hook system configuration (optional)
    system_template: Optional[str] = None  # Path to system prompt template file
    system_prompt: Optional[str] = None  # Inline system prompt (alternative to system_template)
    # Packaged knowledge appended to the system prompt.
    # Bare list allowed: `skills: ["house-style"]`.
    skills: Optional[SkillsConfig] = None
    template_vars: Optional[Dict[str, Any]] = None  # Custom variables for Jinja2 template rendering
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)  # Timeout configuration for deadlock prevention
    loop_detection: LoopDetectionConfig = Field(default_factory=LoopDetectionConfig)  # Tool call loop detection
    # Reasoning loop detection: a model stuck inside its own thinking. Works
    # on the streaming path only, and reads whatever the client reports as a
    # thinking delta — raw reasoning for most models, a summary of it for the
    # OpenAI family.
    reasoning_loop: ReasoningLoopConfig = Field(default_factory=ReasoningLoopConfig)
    # Auto-escalate to the advanced model (llm_profile_advanced[0]) when the run
    # loop observes the agent is stuck (loop detector, or repeated all-error tool
    # steps). Time-boxed + budget-capped; needs a non-empty llm_profile_advanced
    # (distinct from the default). No-op when already running advanced.
    auto_escalate_on_stuck: bool = False
    escalate_rounds: int = 2          # steps to stay on the advanced model per trigger
    escalate_max_calls: int = 6       # total advanced calls allowed per run (budget)
    escalate_error_streak: int = 2    # trigger after N consecutive all-error tool steps
    # How often in a row a TEXT answer cut off at the output cap (finish_reason
    # length, no tool call left) is sent back with a note instead of delivered
    # as the reply. 0 delivers it, as always -- right for an agent whose product
    # is its text, and for a model that loops until the cap. For an agent that
    # works through tool calls the cut is typically a lost call writing a large
    # file (measured in the coder: three in a row, each ending the run).
    output_cap_notes: int = 0

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
                    f"llm_params{where}: identity fields {sorted(protected)} are locked "
                    f"(choose the model with llm_profile / llm.yaml)"
                )
            unknown = set(params) - allowed
            if unknown:
                raise ValueError(
                    f"llm_params{where}: keys not allowed {sorted(unknown)} -- "
                    f"allowed: {sorted(allowed)}"
                )
            # types and values against the real model schema: fail at config load,
            # not at the first LLM call
            LLMModelConfig.model_validate({"model": "_llm_params_probe_", **params})

        # the shape: parameter names (LLMModelConfig fields) are a flat entry,
        # anything else ("*", profile names) a profile key; both at once is invalid
        flat_keys = [k for k in v if k in model_fields]
        profile_keys = [k for k in v if k not in model_fields]
        if flat_keys and profile_keys:
            # a scalar under a non-field key cannot be a profile entry: a typo in
            # flat params, not a mixed shape -- say which keys are allowed
            typo_keys = [k for k in profile_keys if not isinstance(v[k], dict)]
            if typo_keys:
                raise ValueError(
                    f"llm_params: keys not allowed {sorted(typo_keys)} -- "
                    f"allowed: {sorted(allowed)}"
                )
            raise ValueError(
                f"llm_params: mixes params {sorted(flat_keys)} with profile keys "
                f"{sorted(profile_keys)} -- either flat ({{param: value}}) OR keyed by "
                f"profile ({{profile: {{param: value}}}}). Type inheritance causes it "
                f"too (parent flat, child keyed): put the parent's flat params under '*'."
            )
        if profile_keys:
            for pk, sub in v.items():
                if not isinstance(sub, dict):
                    raise ValueError(
                        f"llm_params: keys not allowed ['{pk}'] -- neither an LLM param "
                        f"(allowed: {sorted(allowed)}) nor a profile key with a dict of params"
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
        # The old positional [standard, advanced] reading is gone. A set
        # llm_profile_fallbacks means an unmigrated YAML: fail loudly rather than
        # run with llm_profile[1] silently meaning a fallback instead of advanced.
        if self.llm_profile_fallbacks:
            raise ValueError(
                "llm_profile_fallbacks was removed. Now: "
                "llm_profile = [primary, fallback1, ...] (a chain) and "
                "llm_profile_advanced = [primary_adv, fallback1_adv, ...]. "
                "Migration: python scripts/migrate_llm_profiles.py"
            )
        return self

    @model_validator(mode="after")
    def _validate_llm_params_profile_keys(self, info: ValidationInfo) -> "AgentConfig":
        # Keyed llm_params: valid keys are "*" plus EVERY member of both chains
        # (primary and fallbacks) -- the params apply to each member ("*"/flat
        # everywhere, the exact entry wins). An unknown key (a typo, an entry
        # left over after a chain changed) would be a silent no-op and fails at
        # load. A chain that merges into the inherited one ("+turbo") is whole
        # only after inheritance: get_tool_server_config validates the merged
        # entry again, and the keys are judged there.
        p = self.llm_params
        if _merges_into_inherited(self.llm_profile) or _merges_into_inherited(self.llm_profile_advanced):
            return self
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

    # Role gate: the lowest account role that may RUN this agent -- from the UI,
    # POST /run and /events, as a sub-agent (SAM), as another agent's tool, from a
    # stategraph machine. None (the default) is no gate, exactly as before.
    # Enforced only while auth is enabled (auth/agent_access.py): without
    # accounts there is no role to compare. Inside agent-cli and agent-run
    # (agent_access.local_operator_trusted) their default user "cli_user" passes
    # every gate while no account holds that name; in the API process it is
    # refused like any name without an account.
    min_role: Optional[Literal["guest", "user", "admin"]] = None


class ToolServerConfig(BaseModel):
    """tool server configuration (matches type comment in mcp.yaml for default_config)"""
    model_config = {"extra": "allow"}  # Allow extra fields for plugin-specific config

    type: str = "basic_agent"   # type of tool-server/agent to use
    enabled: bool = False       # enable or disable this tool-server/agent
    description: Optional[str] = None  # Human-readable description of this instance
    self_tool_descriptions: Optional[Dict[str, str]] = Field(default_factory=dict)  # Custom descriptions for this server's own tools
    agent_config: Optional[AgentConfig] = None
    metadata: Optional[AgentMetadata] = None  # Instance metadata (author, version, visibility)


class PluginsConfig(BaseModel):
    """Configuration for local plugins (matches config/plugins.yaml)"""
    plugin_dirs: List[str] = Field(default_factory=list)
    default_config: ToolServerConfig = Field(default_factory=ToolServerConfig)
    servers: Dict[str, ToolServerConfig] = Field(default_factory=dict)  # Named tool server configurations
