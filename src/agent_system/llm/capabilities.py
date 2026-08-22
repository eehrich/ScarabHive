"""LLM model capabilities tracking system.

This module defines capabilities for different LLM models including support for:
- Tools/function calling
- Image input (vision)
- Audio input  
- Video input
- Multimodal features

Capabilities are loaded from configuration files (llm.yaml) rather than hardcoded.
"""
from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel, Field
from enum import Enum
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Global registry loaded from configuration
_capabilities_registry: dict[str, "ModelCapabilities"] = {}


class ModelCapability(str, Enum):
    """Enum of supported LLM capabilities."""
    TOOLS = "tools"
    FUNCTION_CALLING = "function_calling"
    IMAGE_INPUT = "image_input"
    AUDIO_INPUT = "audio_input"
    VIDEO_INPUT = "video_input"
    STREAMING = "streaming"
    JSON_MODE = "json_mode"


class OpenAIApiType(str, Enum):
    """OpenAI API types that a model can use."""
    CHAT_COMPLETIONS = "chat_completions"  # Standard chat API (default)
    ASSISTANTS = "assistants"  # Assistants API with built-in memory
    REALTIME = "realtime"  # Realtime API for speech-to-speech


class ImageFormat(str, Enum):
    """Supported image formats."""
    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    WEBP = "webp"
    BMP = "bmp"
    TIFF = "tiff"
    PDF = "pdf"
    RAW = "raw"
    ICO = "ico"


class ModelCapabilities(BaseModel):
    """Capabilities and limits for a specific LLM model."""
    
    # Feature support flags
    tools: bool = False
    function_calling: bool = False  # Synonym for tools
    image_input: bool = False
    audio_input: bool = False
    video_input: bool = False
    streaming: bool = True  # Most models support streaming
    json_mode: bool = False
    
    # OpenAI API configuration
    supported_api_types: List[OpenAIApiType] = Field(
        default_factory=lambda: [OpenAIApiType.CHAT_COMPLETIONS],
        description="API types this model supports (chat_completions, assistants, realtime, etc.)"
    )
    default_api_type: OpenAIApiType = Field(
        default=OpenAIApiType.CHAT_COMPLETIONS,
        description="Default API type to use for this model"
    )
    
    # Image input configuration
    max_image_size: Optional[int] = None  # bytes
    max_image_resolution: Optional[tuple[int, int]] = None  # (width, height)
    min_image_resolution: Optional[tuple[int, int]] = None  # (width, height)
    supported_image_formats: List[ImageFormat] = Field(default_factory=list)
    image_detail_control: bool = False  # GPT-5 specific
    
    # Audio input configuration
    max_audio_size: Optional[int] = None  # bytes
    max_audio_duration: Optional[int] = None  # seconds
    supported_audio_formats: List[str] = Field(default_factory=list)
    
    # Video input configuration
    max_video_size: Optional[int] = None  # bytes
    max_video_duration: Optional[int] = None  # seconds
    supported_video_formats: List[str] = Field(default_factory=list)
    
    # Provider-specific features
    supports_files_api: bool = False  # Anthropic Files API
    supports_file_uploads: bool = False  # Google File API
    
    def has_capability(self, capability: ModelCapability | str) -> bool:
        """Check if model has a specific capability."""
        if isinstance(capability, str):
            capability = ModelCapability(capability)
        
        return getattr(self, capability.value, False)
    
    def supports_api_type(self, api_type: OpenAIApiType | str) -> bool:
        """Check if model supports a specific OpenAI API type."""
        if isinstance(api_type, str):
            try:
                api_type = OpenAIApiType(api_type)
            except ValueError:
                return False
        
        return api_type in self.supported_api_types
    
    def supports_multimodal(self) -> bool:
        """Check if model supports any multimodal input."""
        return self.image_input or self.audio_input or self.video_input
    
    def supports_image_format(self, format: str) -> bool:
        """Check if model supports a specific image format."""
        try:
            img_format = ImageFormat(format.lower())
            return img_format in self.supported_image_formats
        except ValueError:
            return False


def load_capabilities_from_config(config_path: Optional[str | Path] = None) -> dict[str, ModelCapabilities]:
    """Load model capabilities from configuration file(s).
    
    Loads capabilities from all included config files (llm.yaml,
    llm_openrouter.yaml, ...) through the merged configuration system.
    
    Args:
        config_path: Optional path to a MASTER config (config.yaml). Its
                     includes are followed. If None, the global merged
                     configuration is used.
    
    Returns:
        Dictionary mapping model names to their capabilities
    """
    capabilities_map = {}
    
    # Always through the merged configuration: config.yaml is the only file
    # read directly, its includes bring llm.yaml, llm_openrouter.yaml and the
    # per-plugin model tables — resolved, with `extends` already folded in.
    try:
        from agent_system.config import load_settings
        config = load_settings(str(config_path)) if config_path else load_settings()
        
        if config and hasattr(config, 'llm_system') and config.llm_system:
            models = config.llm_system.models or {}
            
            for model_name, model_config in models.items():
                # Extract capabilities from model config
                caps_data = {}
                if hasattr(model_config, 'capabilities') and model_config.capabilities:
                    caps_obj = model_config.capabilities
                    # Convert Pydantic model to dict
                    for field in ['tools', 'function_calling', 'streaming', 'json_mode',
                                  'image_input', 'audio_input', 'video_input', 'multimodal']:
                        if hasattr(caps_obj, field):
                            value = getattr(caps_obj, field)
                            if value is not None:
                                # Map 'multimodal' to individual capabilities
                                if field == 'multimodal' and value:
                                    caps_data['image_input'] = True
                                    caps_data['audio_input'] = True
                                    caps_data['video_input'] = True
                                else:
                                    caps_data[field] = value
                
                # Convert to ModelCapabilities instance
                capabilities = ModelCapabilities(**caps_data)
                capabilities_map[model_name] = capabilities
                
                # Also register by the actual model string (e.g., "gpt-5" from model: gpt-5)
                actual_model = model_config.model if hasattr(model_config, 'model') else None
                if actual_model and actual_model != model_name:
                    capabilities_map[actual_model] = capabilities
            
            logger.info("Loaded capabilities for %d models from merged config", len(capabilities_map))
            return capabilities_map
            
    except Exception as e:
        logger.debug("Could not load from merged config (%s), falling back to file loading", e)
    
    # No second reader: config.yaml is the only file read directly, everything
    # else arrives through its includes. A single llm.yaml would miss the 36
    # models in llm_openrouter.yaml and every unresolved `extends` — a registry
    # that is quietly half full is worse than an empty one.
    logger.error("Capabilities registry stays empty: the merged configuration "
                 "could not be loaded")
    return capabilities_map


def init_capabilities_registry(config_path: Optional[str | Path] = None) -> None:
    """Initialize the global capabilities registry from configuration.
    
    Args:
        config_path: Optional master config (config.yaml). If None, the
                     global merged configuration is used.
    """
    global _capabilities_registry
    _capabilities_registry = load_capabilities_from_config(config_path)


def register_model_capabilities(model_name: str, capabilities: ModelCapabilities) -> None:
    """Register capabilities for a specific model at runtime.
    
    Args:
        model_name: Name of the model
        capabilities: ModelCapabilities instance
    """
    global _capabilities_registry
    _capabilities_registry[model_name] = capabilities
    logger.debug("Registered capabilities for model: %s", model_name)


def get_model_capabilities(model_name: str) -> ModelCapabilities:
    """Get capabilities for a specific model from the registry.
    
    Args:
        model_name: Name of the model (e.g., "gpt-5", "claude-sonnet-4.5")
    
    Returns:
        ModelCapabilities instance for the model, or default capabilities if not configured
    """
    global _capabilities_registry
    
    # Ensure registry is loaded
    if not _capabilities_registry:
        init_capabilities_registry()
    
    # Try exact match first
    if model_name in _capabilities_registry:
        return _capabilities_registry[model_name]
    
    # Log warning for unconfigured model
    logger.warning(
        "Model '%s' not found in capabilities registry. "
        "Add capabilities to config/llm.yaml. Using default capabilities.",
        model_name
    )
    
    # Default: basic text-only model with tool support
    return ModelCapabilities(
        tools=True,
        function_calling=True,
        streaming=True,
        json_mode=False,
        image_input=False,
        audio_input=False,
        video_input=False
    )


def validate_capability_request(
    model_name: str,
    required_capability: ModelCapability | str
) -> tuple[bool, Optional[str]]:
    """Validate if a model supports a required capability.
    
    Args:
        model_name: Name of the model
        required_capability: Capability to check
    
    Returns:
        Tuple of (is_supported, error_message)
    """
    capabilities = get_model_capabilities(model_name)
    
    if isinstance(required_capability, str):
        try:
            required_capability = ModelCapability(required_capability)
        except ValueError:
            return False, f"Unknown capability: {required_capability}"
    
    if not capabilities.has_capability(required_capability):
        return False, f"Model '{model_name}' does not support {required_capability.value}"
    
    return True, None


def get_compatible_models(required_capability: ModelCapability | str) -> List[str]:
    """Get list of models that support a specific capability.
    
    Args:
        required_capability: Capability to check
    
    Returns:
        List of model names that support the capability
    """
    global _capabilities_registry
    
    # Ensure registry is loaded
    if not _capabilities_registry:
        init_capabilities_registry()
    
    if isinstance(required_capability, str):
        required_capability = ModelCapability(required_capability)
    
    compatible = []
    for model_name, capabilities in _capabilities_registry.items():
        if capabilities.has_capability(required_capability):
            compatible.append(model_name)
    
    return compatible
