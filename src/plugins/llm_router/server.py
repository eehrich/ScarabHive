from __future__ import annotations

from typing import Any

from agent_system.llm.models import ChatMessage
from agent_system.llm.clients import make_llm
from agent_system.mcp.schema_based import SchemaBasedMCPServer
from agent_system.utils.text_sanitizer import sanitize_for_llm


class LLMRouterServer(SchemaBasedMCPServer):
    def __init__(self, name: str, config: dict | None = None, ssl_verify: bool = True) -> None:
        super().__init__(name, config, ssl_verify=ssl_verify)
        self.config = config or {}
        
        # Get parent LLM configuration for profile-based routing
        self.parent_llm = self.config.get("parent_llm", {})



    def _resolve_profile_config(self, profile_name: str) -> dict:
        """Resolve LLM configuration from profile name using parent LLM system."""
        if not isinstance(self.parent_llm, dict) or not self.parent_llm.get('llm_system'):
            raise ValueError("Profile-based routing requires LLM system configuration")
        
        try:
            # Import using relative paths to avoid import issues
            import sys
            import os
            # Add the src directory to Python path for imports
            src_path = os.path.join(os.path.dirname(__file__), '..', '..', '..')
            if src_path not in sys.path:
                sys.path.insert(0, src_path)
                
            from agent_system.config.models import AgentConfig, LLMSystemConfig
            from agent_system.llm.factory import resolve_llm_config_for_agent
            
            # Create AgentConfig from parent_llm dictionary  
            temp_config = AgentConfig(
                llm_system=LLMSystemConfig(**(self.parent_llm.get('llm_system', {}))),
                agent_llm_profiles={f"llm_router_{profile_name}": profile_name},
            )
            
            # Resolve profile to get LLM kwargs
            return resolve_llm_config_for_agent(temp_config, f"llm_router_{profile_name}")
        except Exception as e:
            raise ValueError(f"Failed to resolve profile '{profile_name}': {e}")

    def _get_profile_details(self) -> dict[str, Any]:
        """Get detailed information about all available LLM profiles."""
        if not isinstance(self.parent_llm, dict) or not self.parent_llm.get('llm_system'):
            raise ValueError("Profile-based routing requires LLM system configuration")
        
        llm_system = self.parent_llm.get('llm_system', {})
        profiles = llm_system.get('profiles', {})
        models = llm_system.get('models', {})
        
        profile_details = {}
        
        for profile_name, profile_config in profiles.items():
            if isinstance(profile_config, dict):
                # Get model reference
                model_ref = profile_config.get('model_ref', 'unknown')
                
                # Look up model details
                model_details = models.get(model_ref, {})
                if isinstance(model_details, dict):
                    provider = model_details.get('provider', 'unknown')
                    model_name = model_details.get('model', model_ref)
                else:
                    provider = 'unknown'
                    model_name = model_ref
                
                profile_details[profile_name] = {
                    'description': profile_config.get('description', 'No description available'),
                    'model_ref': model_ref,
                    'provider': provider,
                    'model': model_name,
                    'max_steps': profile_config.get('max_steps', 'unlimited'),
                    'config': profile_config
                }
            else:
                # Handle simple string descriptions
                profile_details[profile_name] = {
                    'description': str(profile_config),
                    'model_ref': 'unknown',
                    'provider': 'unknown', 
                    'model': 'unknown',
                    'max_steps': 'unlimited',
                    'config': profile_config
                }
        
        return profile_details

    def _make_client(self, profile: str):
        """Create an LLM client using profile-based configuration."""
        try:
            llm_kwargs = self._resolve_profile_config(profile)
            return make_llm(
                llm_kwargs["provider"],
                llm_kwargs["model"],
                llm_kwargs["openai_api_key"],
                llm_kwargs["ollama_url"],
                llm_kwargs["context_window"],
                llm_kwargs["ollama_mode"],
                llm_kwargs["request_timeout"],
                ssl_verify=self.ssl_verify,
                httpx_timeouts=llm_kwargs.get("httpx_timeouts"),
            )
        except Exception as e:
            raise ValueError(f"Failed to create LLM client for profile '{profile}': {e}")

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        status = params.get("_status")

        # Only accept the new 'chat_agent' tool name
        if tool == "chat_agent":
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
                messages = [ChatMessage(role="user", content=sanitize_for_llm(params["message"]))]
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

        elif tool == "list_profiles":
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

        raise ValueError(f"Unknown tool: {tool}")



    def get_template_vars(self) -> dict[str, Any]:
        """Provide custom template variables for LLM router schema."""
        # Extract available profiles and models from parent LLM configuration
        available_profiles = []
        available_models = []
        available_providers = []
        
        if isinstance(self.parent_llm, dict) and self.parent_llm.get('llm_system'):
            llm_system = self.parent_llm.get('llm_system', {})
            
            # Get available profiles
            profiles = llm_system.get('profiles', {})
            available_profiles = list(profiles.keys())
            
            # Get available models 
            models = llm_system.get('models', {})
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



    def get_default_action(self) -> str:
        """Return the default action for LLM router."""
        return "chat_agent"
