from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, TYPE_CHECKING

from agent_system.llm import hook_notify
from agent_system.llm.models import ChatMessage
from agent_system.tools.schema_based import SchemaBasedToolServer
from agent_system.llm.text_sanitizer import sanitize_for_llm

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig


class LLMRouterServer(SchemaBasedToolServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        super().__init__(name, system_config, server_config)
        
        # Store the full system config for LLM routing
        self.llm_config = system_config.llm_system
        # NO self.agent_config alias here: it held the SYSTEM config under an
        # agent name, and _resolve_profile_config duly handed it to
        # resolve_llm_config_for_agent() as the first argument with a plain
        # string as the second. The base class already exposes system_config.
        # _make_client passes self.ssl_verify onward, but the base
        # class never sets it - without this every chat() call raised
        # AttributeError (swallowed by the broad except, so the tool was
        # silently non-functional). Mirror basic_agent/server.py.
        network_cfg = getattr(system_config, 'network', None)
        self.ssl_verify = getattr(network_cfg, 'ssl_verify', True) if network_cfg else True



    def _make_serializable(self, obj) -> Any:
        """Convert objects to JSON-serializable format."""
        if hasattr(obj, 'model_dump'):
            # Pydantic v2
            return obj.model_dump()
        elif hasattr(obj, 'dict'):
            # Pydantic v1
            return obj.dict()
        elif isinstance(obj, dict):
            # Recursively handle nested dictionaries
            return {k: self._make_serializable(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            # Recursively handle lists/tuples
            return [self._make_serializable(item) for item in obj]
        elif isinstance(obj, (str, int, float, bool, type(None))):
            # Basic JSON-serializable types
            return obj
        else:
            # Fallback to string representation for unknown objects
            return str(obj)

    def _get_profile_details(self) -> dict[str, Any]:
        """Get detailed information about all available LLM profiles."""
        if not self.llm_config:
            raise ValueError("Profile-based routing requires LLM system configuration")
        
        profiles = self.llm_config.profiles or {}
        models = self.llm_config.models or {}
        
        profile_details = {}
        
        for profile_name, profile_config in profiles.items():
            # Make profile_config serializable early
            serializable_config = self._make_serializable(profile_config)
            
            if isinstance(serializable_config, dict):
                # Get model reference
                model_ref = serializable_config.get('model_ref', 'unknown')
                
                # Look up model details
                model_details: Dict[str, Any] = models.get(model_ref, {})
                # Make model_details serializable too
                serializable_model_details = self._make_serializable(model_details)
                
                if isinstance(serializable_model_details, dict):
                    provider = serializable_model_details.get('provider', 'unknown')
                    model_name = serializable_model_details.get('model', model_ref)
                else:
                    provider = 'unknown'
                    model_name = model_ref
                
                profile_details[profile_name] = {
                    'description': serializable_config.get('description', 'No description available'),
                    'model_ref': model_ref,
                    'provider': provider,
                    'model': model_name,
                }
            else:
                # Handle simple string descriptions
                profile_details[profile_name] = {
                    'description': str(serializable_config),
                    'model_ref': 'unknown',
                    'provider': 'unknown', 
                    'model': 'unknown',
                }
        
        return profile_details

    def _provider_of(self, profile: str) -> str:
        """The provider of the profile's model entry, as llm.yaml names it.

        Not the client's own attribute: clients name themselves unevenly
        (openai_httpx's says "openai", most say nothing). Never raises -- the
        answer it labels has already been paid for.
        """
        try:
            return self._get_profile_details()[profile]["provider"]
        except Exception:
            return "unknown"

    def _make_client(self, profile: str):
        """Create an LLM client for one profile.

        create_llm_from_profile forwards every resolved field and handles
        batch wrapping. The previous route resolved the config by hand - with
        the arguments swapped, so it raised on every call - and then dropped
        thinking_level, max_tokens, safety_settings, service_tier,
        provider_routing and capabilities on the way to the client factory.
        """
        from agent_system.llm.factory import create_llm_from_profile

        try:
            return create_llm_from_profile(
                self.system_config, profile, ssl_verify=self.ssl_verify)
        except Exception as e:
            raise ValueError(
                f"Failed to create LLM client for profile '{profile}': {e}")

    async def chat(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle consultant-style chat requests via LLM profiles.
        
        Tool name: {{ name }}_chat → e.g., 'llm_router_chat'
        Method called after dispatcher strips prefix → 'chat'
        """
        status = params["_status"]
        
        # Check for cancellation before LLM routing
        cancellation_token = params.get("_cancellation_token")
        if cancellation_token and cancellation_token.is_cancelled:
            return {"error": "LLM routing request cancelled by user", "cancelled": True,
                    "forced": cancellation_token.is_forced}

        # Handle both message formats first. A malformed argument answers an
        # error instead of raising, and an empty one counts as missing: it
        # used to reach the provider as an empty prompt.
        raw_messages = params.get("messages")
        text = params.get("message")
        if raw_messages:
            if not isinstance(raw_messages, list):
                return {"error": "messages must be a list of {role, content} objects"}
            messages = []
            for i, m in enumerate(raw_messages):
                try:
                    msg = ChatMessage(**m)
                except (TypeError, ValueError):
                    return {"error": f"messages[{i}] is not a {{role, content}} object"}
                # Only text is sanitized: the sanitizer turns a list of
                # content parts into an empty string.
                if isinstance(msg.content, str):
                    msg.content = sanitize_for_llm(msg.content)
                messages.append(msg)
        elif isinstance(text, str) and text:
            messages = [ChatMessage(role="user", content=sanitize_for_llm(text), timestamp=datetime.now(timezone.utc))]
        else:
            return {"error": "No message or messages provided"}

        # Extract profile parameter (required)
        profile = params.get("profile")
        if not profile:
            return {"error": "Profile parameter is required"}

        try:
            await status.progress(f"Chat request using profile '{profile}'")

            # Create client using profile-based configuration
            client = self._make_client(profile=profile)
            _book_as_agentless(client, params.get("_session_id") or "")

            content = await client.chat(messages, cancellation_token=cancellation_token)

            # The end line repeated the progress line: neither the model
            # that actually served the call nor the response size, although
            # both are read two lines below.
            # Outcome first: the model id can be long ("anthropic/claude-...")
            # and the WebUI cuts the line on the right.
            await status.end(
                f"{len(content or '')} chars via '{profile}' "
                f"-- {getattr(client, 'model', 'unknown')}")
            return {
                "content": content,
                "profile": profile,
                "provider": self._provider_of(profile),
                "model": getattr(client, 'model', 'unknown')
            }
        except asyncio.CancelledError:
            # The user's cancel reaches the client as CancelledError. Letting
            # it out of the tool makes the agent server answer the model with
            # "force-cancelled", so it is answered here -- the same result as
            # the check before the call, reported the same way: the scope stays
            # open on purpose, and the tool base turns the error result into an
            # error event (a cancel is not a completed call).
            # The token here is the TOOL's (tool_execution.py), and a forced
            # termination never marks it: the manager forces the MAIN token and
            # cancels this task, so the kill arrives as an ordinary cancel with
            # `is_forced` false. Answering it ends the tool right here -- the
            # provider call is already gone with the same CancelledError -- so
            # nothing survives the kill, which is what the rule protects.
            if cancellation_token and cancellation_token.is_cancelled:
                return {"error": "LLM routing request cancelled by user", "cancelled": True,
                        "forced": cancellation_token.is_forced}
            raise
        except Exception as e:
            return {
                "error": f"Chat failed with profile '{profile}': {str(e)}",
                "profile": profile
            }

    async def list_profiles(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        List available LLM profiles.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        status = params["_status"]
        
        try:
            await status.progress("Retrieving available LLM profiles")

            profile_details = self._get_profile_details()

            await status.end(f"Retrieved {len(profile_details)} profile(s)")
            return {
                "profiles": profile_details,
                "total_count": len(profile_details)
            }
        except Exception as e:
            return {"error": f"Failed to list profiles: {str(e)}"}


    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for LLM router schema."""
        # Extract available profiles and models from parent LLM configuration
        available_profiles = []
        available_models = []
        available_providers = []
        
        if self.llm_config:
            llm_system = self.llm_config
            
            # Get available profiles
            profiles = getattr(llm_system, 'profiles', {}) or {}
            available_profiles = list(profiles.keys())
            
            # Get available models 
            models = getattr(llm_system, 'models', {}) or {}
            available_models = list(models.keys())
            
            # Get available providers from models
            providers_set = set()
            for model_config in models.values():
                if isinstance(model_config, dict) and 'provider' in model_config:
                    providers_set.add(model_config['provider'])
            available_providers = sorted(list(providers_set))
        
        return {
            "name": self.name,
            "available_profiles": available_profiles,
            "available_models": available_models, 
            "available_providers": available_providers
        }


def _book_as_agentless(client: Any, session_id: str) -> None:
    """Report the client's requests through llm/hook_notify.py.

    No agent wires a client built here, so without this its requests reached
    no pre_llm_request/post_llm_response hook: the message debugger, otel and
    the usage tracker never saw them, and their cost counted nowhere. The
    agent-less path is the right one, not the calling agent's hooks: the
    tracker books an agent-less call as spend of its own, and would skip one
    carrying the agent -- whose own ledger never sees this call.
    """
    if not hasattr(client, "set_llm_hooks"):
        return

    async def on_request(info: dict) -> None:
        await hook_notify.notify_request(
            provider=info.get("provider") or "unknown", model=info.get("model") or "",
            url=info.get("url") or "", payload=info.get("payload"), session_id=session_id)

    async def on_response(info: dict) -> None:
        await hook_notify.notify_response(
            provider=info.get("provider") or "unknown", model=info.get("model") or "",
            url=info.get("url") or "", duration_ms=info.get("duration_ms") or 0.0,
            response_data=info.get("response_data"), usage=info.get("usage"),
            session_id=session_id, error=info.get("error"),
            finish_reason=info.get("finish_reason"),
            metadata={"served_by": (info.get("routing") or {}).get("selected")})

    client.set_llm_hooks(on_pre_request=on_request, on_post_response=on_response)
