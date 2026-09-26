"""
Multimodal processing utilities for LLM inputs.

Provides functions to load, validate, and encode images, audio, and text files
for use with multimodal LLM models across CLI, API, and agent interfaces.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import List, Optional, Union

from PIL import Image as PILImage

from ..llm.models import ChatMessage, TextContent, ImageContent, AudioContent, TextFileContent


logger = logging.getLogger(__name__)


class ImageProcessingError(Exception):
    """Raised when image processing fails."""
    pass


class AudioProcessingError(Exception):
    """Raised when audio processing fails."""
    pass


class TextFileProcessingError(Exception):
    """Raised when text file processing fails."""
    pass


# Supported file types
#
# The image set carries the other spellings of formats it already names --
# jfif and jpe are JPEG, tif is TIFF. Only detect_file_type reads this set;
# validate_image_file never looks at the extension, it opens the file with
# PIL. So a `screenshot.jfif` (what Chrome's "save image as" produces) built
# a perfectly good `data:image/jpeg` message while the detector called it
# unknown, and `scan.tiff` was an image where `scan.tif` was not.
SUPPORTED_IMAGE_FORMATS = {'jpeg', 'jpg', 'jfif', 'jpe', 'png', 'gif', 'webp',
                           'bmp', 'tiff', 'tif'}
SUPPORTED_AUDIO_FORMATS = {'mp3', 'wav', 'ogg', 'flac', 'm4a', 'webm', 'aac'}
SUPPORTED_TEXT_EXTENSIONS = {
    '.txt', '.md', '.csv', '.json', '.xml', '.html', '.htm',
    '.py', '.js', '.ts', '.css', '.yaml', '.yml', '.log', '.ini',
    '.cfg', '.conf', '.sh', '.bat', '.ps1', '.sql', '.r', '.java',
    '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.rb', '.swift', '.kt'
}


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


def validate_audio_file(file_path: Path | str) -> tuple[str, int]:
    """
    Validate that a file is a valid audio file.
    
    Args:
        file_path: Path to the audio file
        
    Returns:
        Tuple of (format, size_bytes)
        
    Raises:
        AudioProcessingError: If file doesn't exist, isn't a file, or isn't a supported audio format
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    if not file_path.exists():
        raise AudioProcessingError(f"Audio file not found: {file_path}")
    
    if not file_path.is_file():
        raise AudioProcessingError(f"Not a file: {file_path}")
    
    # Check extension
    ext = file_path.suffix.lower().lstrip('.')
    if ext not in SUPPORTED_AUDIO_FORMATS:
        raise AudioProcessingError(
            f"Unsupported audio format: {ext}. Supported: {', '.join(SUPPORTED_AUDIO_FORMATS)}"
        )
    
    size_bytes = file_path.stat().st_size
    return ext, size_bytes


def encode_audio_to_data_url(
    file_path: Path | str,
    max_size_mb: Optional[float] = None,
    warn_size_mb: float = 10.0
) -> tuple[str, dict]:
    """
    Encode an audio file to a base64 data URL.
    
    Args:
        file_path: Path to the audio file
        max_size_mb: Maximum allowed size in MB (None = no limit)
        warn_size_mb: Size threshold for logging warning
        
    Returns:
        Tuple of (data_url, metadata) where metadata contains format, size_mb, duration_seconds
        
    Raises:
        AudioProcessingError: If encoding fails or size exceeds max_size_mb
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    audio_format, size_bytes = validate_audio_file(file_path)
    size_mb = size_bytes / (1024 * 1024)
    
    if max_size_mb and size_mb > max_size_mb:
        raise AudioProcessingError(
            f"Audio {file_path} size {size_mb:.1f} MB exceeds maximum {max_size_mb} MB"
        )
    
    if size_mb > warn_size_mb:
        logger.warning(
            f"Audio {file_path} is {size_mb:.1f} MB, may exceed model limits"
        )
    
    # Get audio duration BEFORE encoding (for accurate token estimation)
    duration_seconds: float | None = None
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(str(file_path))
        duration_seconds = len(audio) / 1000.0  # pydub returns milliseconds
        logger.debug(f"Audio duration: {duration_seconds:.1f}s for {file_path}")
    except Exception as e:
        logger.debug(f"Could not get audio duration for {file_path}: {e}")
        # Fallback: estimate from file size (~16KB per second for typical audio)
        duration_seconds = size_bytes / (16 * 1024)
    
    # Read and encode
    try:
        with open(file_path, "rb") as f:
            audio_data = f.read()
        b64_data = base64.b64encode(audio_data).decode("utf-8")
        
        # Get proper MIME type
        mime_type = mimetypes.guess_type(str(file_path))[0] or f"audio/{audio_format}"
        data_url = f"data:{mime_type};base64,{b64_data}"
        
    except Exception as e:
        raise AudioProcessingError(f"Failed to encode audio {file_path}: {e}") from e
    
    metadata = {
        "format": audio_format,
        "size_mb": size_mb,
        "path": str(file_path),
        "mime_type": mime_type,
        "duration_seconds": duration_seconds,
    }
    
    return data_url, metadata


def validate_text_file(file_path: Path | str) -> tuple[str, int]:
    """
    Validate that a file is a valid text file.
    
    Args:
        file_path: Path to the text file
        
    Returns:
        Tuple of (extension, size_bytes)
        
    Raises:
        TextFileProcessingError: If file doesn't exist, isn't a file, or isn't a supported text format
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    if not file_path.exists():
        raise TextFileProcessingError(f"Text file not found: {file_path}")
    
    if not file_path.is_file():
        raise TextFileProcessingError(f"Not a file: {file_path}")
    
    # Check extension
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_TEXT_EXTENSIONS:
        raise TextFileProcessingError(
            f"Unsupported text format: {ext}. Supported: {', '.join(sorted(SUPPORTED_TEXT_EXTENSIONS))}"
        )
    
    size_bytes = file_path.stat().st_size
    return ext, size_bytes


def read_text_file(
    file_path: Path | str,
    max_size_mb: Optional[float] = 5.0,
    encoding: str = "utf-8"
) -> tuple[str, dict]:
    """
    Read a text file content.
    
    Args:
        file_path: Path to the text file
        max_size_mb: Maximum allowed size in MB (default 5MB for text)
        encoding: Text encoding to use
        
    Returns:
        Tuple of (content, metadata) where metadata contains extension, size_mb, lines
        
    Raises:
        TextFileProcessingError: If reading fails or size exceeds max_size_mb
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    ext, size_bytes = validate_text_file(file_path)
    size_mb = size_bytes / (1024 * 1024)
    
    if max_size_mb and size_mb > max_size_mb:
        raise TextFileProcessingError(
            f"Text file {file_path} size {size_mb:.1f} MB exceeds maximum {max_size_mb} MB"
        )
    
    # Read content
    try:
        content = file_path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        # Try with latin-1 as fallback
        try:
            content = file_path.read_text(encoding="latin-1")
            logger.warning(f"File {file_path} read with latin-1 encoding (utf-8 failed)")
        except Exception as e:
            raise TextFileProcessingError(f"Failed to read text file {file_path}: {e}") from e
    except Exception as e:
        raise TextFileProcessingError(f"Failed to read text file {file_path}: {e}") from e
    
    lines = content.count('\n') + 1
    
    metadata = {
        "extension": ext,
        "size_mb": size_mb,
        "path": str(file_path),
        "lines": lines,
        "encoding": encoding
    }
    
    return content, metadata


def create_multimodal_message_extended(
    text: str,
    image_paths: Optional[List[Path | str]] = None,
    audio_paths: Optional[List[Path | str]] = None,
    text_file_paths: Optional[List[Path | str]] = None,
    max_image_size_mb: Optional[float] = None,
    max_audio_size_mb: Optional[float] = None,
    max_text_size_mb: Optional[float] = 5.0
) -> ChatMessage:
    """
    Create a ChatMessage with text, images, audio, and text file content.
    
    This is an extended version that supports all multimodal input types.
    
    Args:
        text: The primary text content/message
        image_paths: List of paths to image files
        audio_paths: List of paths to audio files  
        text_file_paths: List of paths to text files (content will be included inline)
        max_image_size_mb: Maximum allowed size per image in MB
        max_audio_size_mb: Maximum allowed size per audio in MB
        max_text_size_mb: Maximum allowed size per text file in MB
        
    Returns:
        ChatMessage with multimodal content
        
    Raises:
        ImageProcessingError: If any image processing fails
        AudioProcessingError: If any audio processing fails
        TextFileProcessingError: If any text file processing fails
    """
    content_list: List[Union[TextContent, ImageContent, AudioContent, TextFileContent]] = []
    
    # Add the main text content first
    content_list.append(TextContent(text=text))
    
    # Process text files as separate TextFileContent items
    if text_file_paths:
        for txt_path in text_file_paths:
            try:
                content, metadata = read_text_file(txt_path, max_size_mb=max_text_size_mb)
                filename = Path(txt_path).name
                content_list.append(TextFileContent(
                    type="text_file",
                    content=content,
                    name=filename
                ))
                logger.debug(
                    f"Attached text file: {metadata['path']} "
                    f"({metadata['extension']}, {metadata['lines']} lines, {metadata['size_mb']:.2f} MB)"
                )
            except TextFileProcessingError:
                raise
            except Exception as e:
                raise TextFileProcessingError(f"Unexpected error processing {txt_path}: {e}") from e
    
    # Process images
    if image_paths:
        for img_path in image_paths:
            try:
                data_url, metadata = encode_image_to_data_url(
                    img_path,
                    max_size_mb=max_image_size_mb
                )
                content_list.append(ImageContent(
                    type="image_url",
                    image_url={"url": data_url},
                    name=metadata.get("original_filename") or Path(img_path).name
                ))
                logger.debug(
                    f"Attached image: {metadata['path']} "
                    f"({metadata['format']}, {metadata['dimensions'][0]}x{metadata['dimensions'][1]}, "
                    f"{metadata['size_mb']:.1f} MB)"
                )
            except ImageProcessingError:
                raise
            except Exception as e:
                raise ImageProcessingError(f"Unexpected error processing {img_path}: {e}") from e
    
    # Process audio files
    if audio_paths:
        for audio_path in audio_paths:
            try:
                data_url, metadata = encode_audio_to_data_url(
                    audio_path,
                    max_size_mb=max_audio_size_mb
                )
                content_list.append(AudioContent(
                    type="audio",
                    audio_url=data_url,
                    media_type=metadata["mime_type"],
                    name=metadata.get("original_filename") or Path(audio_path).name,
                    duration_seconds=metadata.get("duration_seconds"),
                ))
                logger.debug(
                    f"Attached audio: {metadata['path']} "
                    f"({metadata['format']}, {metadata['size_mb']:.1f} MB, "
                    f"{metadata.get('duration_seconds', 0):.1f}s)"
                )
            except AudioProcessingError:
                raise
            except Exception as e:
                raise AudioProcessingError(f"Unexpected error processing {audio_path}: {e}") from e
    
    return ChatMessage(role="user", content=content_list)  # type: ignore[arg-type]


class AttachmentRejected(ValueError):
    """The attachments cannot go out with this run: the model cannot take
    them, or a file could not be read. The message says which; each entry
    point turns it into its own answer (HTTP 400, exit 1, "Not sent")."""


def message_with_attachments(text: str, kinds: dict, llm_override: object,
                             agent: object) -> Union[str, ChatMessage]:
    """*text* plus the sorted attachments as one message, *text* alone without any.

    *kinds* maps "image", "audio" and "text" to paths (the shape
    sort_attachments returns; a missing key is empty). Checked against the
    model the attachments will reach -- the per-request override wins over
    the agent's default, one rule for the HTTP API, the chat and both
    command-line entry points. Raises AttachmentRejected.
    """
    from ..llm.capabilities import capability_model_name, ensure_model_supports

    images, audio, texts = (list(kinds.get(kind) or []) for kind in ("image", "audio", "text"))
    if not (images or audio or texts):
        return text
    problem = ensure_model_supports(capability_model_name(llm_override, agent),
                                    images=len(images), audio=len(audio))
    if problem:
        raise AttachmentRejected(problem)
    try:
        message = create_multimodal_message_extended(
            text=text, image_paths=images or None, audio_paths=audio or None,
            text_file_paths=texts or None)
    except (ImageProcessingError, AudioProcessingError, TextFileProcessingError) as e:
        raise AttachmentRejected(str(e)) from e
    logger.info("Attachments: %d image(s), %d audio(s), %d text file(s)",
                len(images), len(audio), len(texts))
    return message


def detect_file_type(file_path: Path | str) -> str:
    """
    Detect the type of a file (image, audio, text, or unknown).
    
    Args:
        file_path: Path to the file
        
    Returns:
        One of: 'image', 'audio', 'text', 'unknown'
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)
    
    ext = file_path.suffix.lower().lstrip('.')
    
    if ext in SUPPORTED_IMAGE_FORMATS:
        return 'image'
    elif ext in SUPPORTED_AUDIO_FORMATS:
        return 'audio'
    elif file_path.suffix.lower() in SUPPORTED_TEXT_EXTENSIONS:
        return 'text'
    else:
        return 'unknown'
