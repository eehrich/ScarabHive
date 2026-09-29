"""Which inputs a model accepts, and the gate that asks before attaching them.

The capability table is built from the merged configuration; nothing here is
hardcoded per model.
"""
from __future__ import annotations

from typing import Optional
from enum import Enum
import logging
from pathlib import Path

from ..config.models import ModelCapabilitiesConfig

logger = logging.getLogger(__name__)

_capabilities_registry: dict[str, "ModelCapabilities"] = {}
_registry_loaded = False

#: What ensure_model_supports() actually reads. Two config entries may share a
#: provider model string; only a disagreement in THESE fields is ambiguous.
_INPUT_FLAGS = ("image_input", "audio_input", "video_input")


class ModelCapability(str, Enum):
    """Enum of supported LLM capabilities."""
    TOOLS = "tools"
    FUNCTION_CALLING = "function_calling"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    VIDEO_INPUT = "video_input"
    STREAMING = "streaming"
    JSON_MODE = "json_mode"
    STRUCTURED_OUTPUT = "structured_output"


class ModelCapabilities(ModelCapabilitiesConfig):
    """The capability set of a model — the config class plus its one query.

    It used to be a second, field-for-field copy of ModelCapabilitiesConfig
    (22 identical fields, some with differing defaults). A new capability had
    to be added twice, and only one of them reached the registry.
    """

    def has_capability(self, capability: "ModelCapability | str") -> bool:
        if isinstance(capability, str):
            capability = ModelCapability(capability)
        return bool(getattr(self, capability.value, False))


def load_capabilities_from_config(config_path: Optional[str | Path] = None) -> dict[str, ModelCapabilities]:
    """Build the registry from the merged configuration.

    config.yaml is the only file read directly; its includes bring llm.yaml,
    llm_openrouter.yaml and the per-plugin tables — resolved, with `extends`
    already folded in. Reading a single file would miss the other models AND
    every unresolved inheritance.
    """
    try:
        from agent_system.config import load_settings
        config = load_settings(str(config_path)) if config_path else load_settings()
    except Exception as e:
        logger.error("Capabilities registry stays empty: the merged "
                     "configuration could not be loaded (%s)", e)
        return {}

    models = getattr(getattr(config, "llm_system", None), "models", None) or {}
    out: dict[str, ModelCapabilities] = {}
    aliases: list[tuple[str, str, ModelCapabilities]] = []
    for name, model in models.items():
        declared = getattr(model, "capabilities", None)
        caps = (ModelCapabilities.model_validate(declared.model_dump())
                if declared is not None else ModelCapabilities())
        out[name] = caps
        actual = getattr(model, "model", None)
        if actual and actual != name:
            aliases.append((actual, name, caps))

    # A model is also reachable by its provider string — callers know either.
    # Second pass, because an entry name always outranks somebody else's alias:
    # 'claude-sonnet-5' is an entry AND the model string of two others.
    alias_owner: dict[str, str] = {}
    ambiguous: set[str] = set()
    for actual, name, caps in aliases:
        if actual in models:
            continue
        owner = alias_owner.get(actual)
        if owner is None:
            alias_owner[actual] = name
            out[actual] = caps
        elif _inputs_of(out[actual]) != _inputs_of(caps):
            # Dict order would decide which answer the gate gives. Say nothing
            # rather than flip a coin about what a model can take.
            ambiguous.add(actual)
            logger.warning("'%s' is claimed by '%s' and '%s' with different "
                           "input capabilities — attachments to it stay "
                           "unchecked", actual, owner, name)
    for actual in ambiguous:
        out.pop(actual, None)
    logger.info("Loaded capabilities for %d models", len(out))
    return out


def _inputs_of(caps: "ModelCapabilities") -> tuple[bool, ...]:
    return tuple(bool(getattr(caps, f, False)) for f in _INPUT_FLAGS)


def init_capabilities_registry(config_path: Optional[str | Path] = None) -> None:
    global _capabilities_registry, _registry_loaded
    _capabilities_registry = load_capabilities_from_config(config_path)
    _registry_loaded = True


def capability_model_name(llm_override: object, agent: object) -> Optional[str]:
    """The model the attachments will actually reach: the per-request override
    wins over the agent's default. One rule for the HTTP API, the chat and
    both command-line entry points."""
    model = getattr(llm_override, "model", None)
    return model or getattr(getattr(agent, "llm", None), "model", None)


def ensure_model_supports(model_name: Optional[str], *, images: int = 0,
                          audio: int = 0, video: int = 0) -> Optional[str]:
    """Check a model against the attachments it is about to receive.

    Returns an error message, or None when the model can take them. Every
    entry point that attaches media must ask — until 2026-08-22 only the HTTP
    API did, so `agent-cli --audio` handed a recording to text-only models and
    the error came back from the provider, late and unspecific.

    An unknown model name yields None: the registry is not complete enough to
    veto a request over a name it has never seen.
    """
    if not model_name:
        return None
    # Once per process, not once per request: load_settings() re-reads every
    # YAML file and drops the plugin and inheritance caches on the way.
    if not _registry_loaded:
        init_capabilities_registry()
    caps = _capabilities_registry.get(model_name)
    if caps is None:
        logger.warning("No capabilities known for '%s' — attachments pass "
                       "unchecked", model_name)
        return None
    for count, flag in ((images, ModelCapability.IMAGE_INPUT),
                        (audio, ModelCapability.AUDIO_INPUT),
                        (video, ModelCapability.VIDEO_INPUT)):
        if count and not caps.has_capability(flag):
            return (f"Model '{model_name}' does not support {flag.value} "
                    f"({count} attachment(s) given)")
    return None
