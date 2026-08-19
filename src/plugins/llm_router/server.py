from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, TYPE_CHECKING

from agent_system.llm.models import ChatMessage
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.llm.text_sanitizer import sanitize_for_llm

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig


class LLMRouterServer(SchemaBasedMCPServer):
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        super().__init__(name, system_config, mcp_config)
        
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
                    'max_steps': serializable_config.get('max_steps', 'unlimited'),
                    'config': serializable_config
                }
            else:
                # Handle simple string descriptions
                profile_details[profile_name] = {
                    'description': str(serializable_config),
                    'model_ref': 'unknown',
                    'provider': 'unknown', 
                    'model': 'unknown',
                    'max_steps': 'unlimited',
                    'config': serializable_config
                }
        
        return profile_details

    def _make_client(self, profile: str):
        """Create an LLM client for one profile.

        create_llm_from_profile forwards every resolved field and handles
        batch wrapping. The previous route resolved the config by hand - with
        the arguments swapped, so it raised on every call - and then dropped
        thinking_level, max_tokens, safety_settings, service_tier,
        provider_routing and capabilities on the way to make_llm.
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
            return {"error": "LLM routing request cancelled by user", "cancelled": True}

        # Handle both message formats first
        if "messages" in params:
            messages = [ChatMessage(**m) for m in params["messages"]]
            # Sanitize message content
            for msg in messages:
                if msg.content:
                    msg.content = sanitize_for_llm(msg.content)
        elif "message" in params:
            messages = [ChatMessage(role="user", content=sanitize_for_llm(params["message"]), timestamp=datetime.now(timezone.utc))]
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

            content = await client.chat(messages, cancellation_token=cancellation_token)

            await status.end(f"Chat completed using profile '{profile}'")
            return {
                "content": content,
                "profile": profile,
                "provider": getattr(client, 'provider', 'unknown'),
                "model": getattr(client, 'model', 'unknown')
            }
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
