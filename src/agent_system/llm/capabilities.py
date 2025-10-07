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
import yaml
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
    """Load model capabilities from configuration file.
    
    Args:
        config_path: Path to llm.yaml config file. If None, tries default locations.
    
    Returns:
        Dictionary mapping model names to their capabilities
    """
    if config_path is None:
        # Try default locations
        possible_paths = [
            Path("config/llm.yaml"),
            Path(__file__).parent.parent.parent.parent / "config" / "llm.yaml",
        ]
        config_path = None
        for path in possible_paths:
            if path.exists():
                config_path = path
                break
        
        if config_path is None:
            logger.warning("No llm.yaml config file found, using empty capabilities registry")
            return {}
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        if not config or 'llm_system' not in config:
            logger.warning("Invalid config structure in %s", config_path)
            return {}
        
        llm_system = config['llm_system']
        models = llm_system.get('models', {})
        
        capabilities_map = {}
        for model_name, model_config in models.items():
            # Extract capabilities from model config
            caps_data = model_config.get('capabilities', {})
            
            # Convert to ModelCapabilities instance
            capabilities = ModelCapabilities(**caps_data)
            capabilities_map[model_name] = capabilities
            
            # Also register by the actual model string (e.g., "gpt-5" from model: gpt-5)
            actual_model = model_config.get('model')
            if actual_model and actual_model != model_name:
                capabilities_map[actual_model] = capabilities
        
        logger.info("Loaded capabilities for %d models from %s", len(capabilities_map), config_path)
        return capabilities_map
    
    except Exception as e:
        logger.error("Error loading capabilities from %s: %s", config_path, e)
        return {}


def init_capabilities_registry(config_path: Optional[str | Path] = None) -> None:
    """Initialize the global capabilities registry from configuration.
    
    Args:
        config_path: Path to llm.yaml config file. If None, uses defaults.
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
