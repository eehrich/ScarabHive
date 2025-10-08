"""I/O utilities for safe file operations."""
import logging
import tempfile
from pathlib import Path
from typing import Union


logger = logging.getLogger(__name__)


def atomic_write_text(
    filepath: Union[str, Path],
    content: str,
    encoding: str = "utf-8"
) -> None:
    """Write text to a file atomically.
    
    Uses a temporary file and rename to ensure atomic writes,
    preventing corruption if interrupted mid-write.
    
    Args:
        filepath: Target file path.
        content: Text content to write.
        encoding: Text encoding (default: utf-8).
    
    Raises:
        OSError: If file write or rename fails.
    """
    filepath = Path(filepath)
    
    try:
        # Create temp file in same directory as target
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding=encoding,
            dir=filepath.parent,
            delete=False,
            suffix='.tmp'
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        
        # Atomic rename
        tmp_path.replace(filepath)
        
        logger.debug("Atomic write successful: %s", filepath)
        
    except Exception as e:
        logger.exception("Atomic write failed: %s, error=%s", filepath, e)
        # Cleanup temp file if it exists
        try:
            if 'tmp_path' in locals():
                tmp_path.unlink(missing_ok=True)
        except Exception as cleanup_error:
            logger.warning("Failed to cleanup temp file: %s", cleanup_error)
        raise
