"""Decision models — resolve a configured profile to a ready client.

A decision model answers NAMED QUESTIONS about a piece of content with a typed
value and a probability: no message list, no prose, no tool calls. The clients
live in plugins under ``src/plugins/`` and declare ``provides_decisions``
in their manifest; dispatch goes through ``registry.build_decisions_client``,
the same seam the TTS clients use.

Only the profile lookup lives here. What a client can do is the plugin's
business — ``plugins.llm_decisions`` documents the question types and the
answer shape.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["create_decisions_from_profile", "decision_fingerprint"]


def _decision_model(config: Any, decision_profile: Optional[str] = None) -> Any:
    """The decision model config *decision_profile* names (None: the default profile). ValueError when the
    profile or its model is not configured."""
    llm_cfg = config.llm_system
    if not llm_cfg:
        raise ValueError("No llm_system configuration found in config.")

    if decision_profile is None:
        decision_profile = llm_cfg.default_decision_profile
        if not decision_profile:
            raise ValueError(
                "No decision profile given and llm_system."
                "default_decision_profile is not set. Available: "
                f"{list(llm_cfg.decision_profiles.keys())}"
            )

    profiles = llm_cfg.decision_profiles
    if decision_profile not in profiles:
        raise ValueError(
            f"Decision profile {decision_profile!r} not found. "
            f"Available: {list(profiles.keys())}"
        )

    model_ref = profiles[decision_profile].model_ref

    models = llm_cfg.decision_models
    if model_ref not in models:
        raise ValueError(
            f"Decision model {model_ref!r} (from profile {decision_profile!r}) "
            f"not found. Available: {list(models.keys())}"
        )

    return models[model_ref]


def create_decisions_from_profile(
    config: Any,
    decision_profile: Optional[str] = None,
) -> Any:
    """Create a decisions client from a named profile in the system config.

    Looks up the profile in ``config.llm_system.decision_profiles``, resolves
    the model reference in ``config.llm_system.decision_models``, and returns a
    configured client via the provider registry.

    Args:
        config: AgentSystemConfig instance.
        decision_profile: Profile name (e.g. "jev"). Omitted, the configured
            ``default_decision_profile`` decides; a caller that cares which
            model judges its content should name it.

    Returns:
        The client its provider plugin builds.

    Raises:
        ValueError: If profile or model not found, or if no profile is named
            and no default is configured.
    """
    # Late import for the same reason as create_tts_from_profile: the seam
    # stays patchable, and provider plugins load lazily on first use.
    from agent_system.llm import registry

    return registry.build_decisions_client(_decision_model(config, decision_profile))


def decision_fingerprint(config: Any, decision_profile: Optional[str] = None, _params: Any = None) -> str:
    """What a decisions client of *decision_profile* is built from, hashed (the model config carries its key);
    the fingerprint of llm.factory.ProfileClients for these clients."""
    from agent_system.llm.factory import _digest

    return _digest(_decision_model(config, decision_profile).model_dump_json())
