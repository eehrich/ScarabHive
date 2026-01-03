"""
Image processing utilities for multimodal LLM inputs.

Provides functions to load, validate, and encode images for use with
vision-capable LLM models across CLI, API, and agent interfaces.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import List, Optional, Union

from PIL import Image as PILImage

from ..llm.models import ChatMessage, TextContent, ImageContent


logger = logging.getLogger(__name__)


class ImageProcessingError(Exception):
    """Raised when image processing fails."""
    pass


def validate_image_file(file_path: Path | str) -> tuple[str, tuple[int, int]]:
    """
    Validate that a file is a valid image.
    
    Args:
        file_path: Path to the image file (Path object or string)
        
    Returns:
        Tuple of (format, dimensions) where format is lowercase (e.g. 'jpeg', 'png')
        and dimensions is (width, height)
        
    Raises:
        ImageProcessingError: If file doesn't exist, isn't a file, or can't be opened as image
    """
    # Convert string to Path if needed
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    if not file_path.exists():
        raise ImageProcessingError(f"Image file not found: {file_path}")
    
    if not file_path.is_file():
        raise ImageProcessingError(f"Not a file: {file_path}")
    
    try:
        with PILImage.open(file_path) as img:
            img_format = img.format.lower() if img.format else "unknown"
            dimensions = img.size
            return img_format, dimensions
    except Exception as e:
        raise ImageProcessingError(f"Cannot open image {file_path}: {e}") from e


def encode_image_to_data_url(
    file_path: Path | str,
    max_size_mb: Optional[float] = None,
    warn_size_mb: float = 10.0
) -> tuple[str, dict]:
    """
    Encode an image file to a base64 data URL.
    
    Args:
        file_path: Path to the image file (Path object or string)
        max_size_mb: Maximum allowed size in MB (None = no limit)
        warn_size_mb: Size threshold for logging warning
        
    Returns:
        Tuple of (data_url, metadata) where metadata contains format, dimensions, size_mb
        
    Raises:
        ImageProcessingError: If encoding fails or size exceeds max_size_mb
    """
    # Convert string to Path if needed (validate_image_file will also do this)
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    # Validate first
    img_format, dimensions = validate_image_file(file_path)
    
    # Read image data
    try:
        with open(file_path, "rb") as f:
            img_data = f.read()
    except Exception as e:
        raise ImageProcessingError(f"Failed to read image file {file_path}: {e}") from e
    
    # Check size
    size_mb = len(img_data) / (1024 * 1024)
    
    if max_size_mb and size_mb > max_size_mb:
        raise ImageProcessingError(
            f"Image {file_path} size {size_mb:.1f} MB exceeds maximum {max_size_mb} MB"
        )
    
    if size_mb > warn_size_mb:
        logger.warning(
            f"Image {file_path} is {size_mb:.1f} MB, may exceed model limits"
        )
    
    # Encode to base64
    try:
        b64_data = base64.b64encode(img_data).decode("utf-8")
        data_url = f"data:image/{img_format};base64,{b64_data}"
    except Exception as e:
        raise ImageProcessingError(f"Failed to encode image {file_path}: {e}") from e
    
    metadata = {
        "format": img_format,
        "dimensions": dimensions,
        "size_mb": size_mb,
        "path": str(file_path)
    }
    
    return data_url, metadata


def create_multimodal_message(
    text: str,
    image_paths: List[Path | str],
    max_size_mb: Optional[float] = None
) -> ChatMessage:
    """
    Create a ChatMessage with text and image content.
    
    Args:
        text: The text content
        image_paths: List of paths to image files (Path objects or strings)
        max_size_mb: Maximum allowed size per image in MB (None = no limit)
        
    Returns:
        ChatMessage with multimodal content (text + images)
        
    Raises:
        ImageProcessingError: If any image processing fails
    """
    # Start with text content
    content_list: List[Union[TextContent, ImageContent]] = [TextContent(text=text)]
    
    # Process each image
    for img_path in image_paths:
        try:
            data_url, metadata = encode_image_to_data_url(
                img_path, 
                max_size_mb=max_size_mb
            )
            
            # Add image content (OpenAI format with dict)
            content_list.append(ImageContent(
                type="image_url",
                image_url={"url": data_url}
            ))
            
            logger.debug(
                f"Attached image: {metadata['path']} "
                f"({metadata['format']}, {metadata['dimensions'][0]}x{metadata['dimensions'][1]}, "
                f"{metadata['size_mb']:.1f} MB)"
            )
            
        except ImageProcessingError:
            # Re-raise as-is
            raise
        except Exception as e:
            # Wrap unexpected errors
            raise ImageProcessingError(f"Unexpected error processing {img_path}: {e}") from e
    
    # Create ChatMessage with multimodal content
    # Cast to expected union type (TextContent | ImageContent is subset of full union)
    return ChatMessage(role="user", content=content_list)  # type: ignore[arg-type]  # List type compatible with union
