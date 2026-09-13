"""Media Store for Context Engineer Plugin.

Stores inline base64 media data to disk before compaction, allowing restoration.
Implements TTL-based cleanup to prevent disk space exhaustion.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MediaStore:
    """Stores media files with TTL-based cleanup.
    
    Media files are stored with metadata including creation time.
    Cleanup runs periodically to remove files older than TTL.
    """
    
    def __init__(
        self,
        storage_path: Path,
        ttl_seconds: int = 86400 * 7,  # 7 days default
        max_files: int = 1000,
        cleanup_interval: int = 3600  # 1 hour
    ):
        """Initialize media store.
        
        Args:
            storage_path: Directory for storing media files
            ttl_seconds: Time-to-live for stored files (default 7 days)
            max_files: Maximum number of files to keep (oldest removed first)
            cleanup_interval: Minimum seconds between cleanup runs
        """
        self.storage_path = Path(storage_path)
        self.ttl_seconds = ttl_seconds
        self.max_files = max_files
        self.cleanup_interval = cleanup_interval
        self._last_cleanup = 0.0
        
        # Create storage directory
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        # Metadata file for tracking stored media
        self._metadata_file = self.storage_path / "metadata.json"
        self._metadata: dict[str, dict[str, Any]] = self._load_metadata()
        
        logger.info(
            f"MediaStore initialized: path={storage_path}, "
            f"ttl={ttl_seconds}s, max_files={max_files}"
        )
    
    def _load_metadata(self) -> dict[str, dict[str, Any]]:
        """Load metadata from disk."""
        if not self._metadata_file.exists():
            return {}
        
        try:
            with open(self._metadata_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load media metadata: {e}")
            return {}
    
    def _save_metadata(self) -> None:
        """Save metadata to disk."""
        try:
            with open(self._metadata_file, 'w', encoding='utf-8') as f:
                json.dump(self._metadata, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save media metadata: {e}")
    
    def _compute_hash(self, data: bytes) -> str:
        """Compute SHA256 hash of data."""
        return hashlib.sha256(data).hexdigest()[:16]
    
    def _get_extension(self, media_type: str) -> str:
        """Get file extension from media type."""
        extensions = {
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/flac": ".flac",
            "audio/x-flac": ".flac",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }
        return extensions.get(media_type, ".bin")
    
    def store(
        self,
        data: str | bytes,
        media_type: str,
        session_id: str,
        source_name: str | None = None
    ) -> str | None:
        """Store media data and return file path.
        
        Args:
            data: Base64 string or raw bytes
            media_type: MIME type (e.g., "audio/flac")
            session_id: Session ID for organization
            source_name: Optional original filename
            
        Returns:
            File path if stored successfully, None on error
        """
        try:
            # Decode if base64 string
            if isinstance(data, str):
                raw_data = base64.b64decode(data)
            else:
                raw_data = data
            
            # Compute hash for deduplication
            data_hash = self._compute_hash(raw_data)
            
            # Check if already stored
            if data_hash in self._metadata:
                existing = self._metadata[data_hash]
                existing_path = Path(existing["path"])
                if existing_path.exists():
                    # Update access time
                    existing["last_accessed"] = time.time()
                    self._save_metadata()
                    logger.debug(f"Media already stored: {existing_path}")
                    return str(existing_path)
            
            # Create session directory
            session_dir = self.storage_path / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate filename
            ext = self._get_extension(media_type)
            if source_name:
                # Use original name with hash suffix for uniqueness
                base_name = Path(source_name).stem
                filename = f"{base_name}_{data_hash}{ext}"
            else:
                filename = f"media_{data_hash}{ext}"
            
            file_path = session_dir / filename
            
            # Write file
            file_path.write_bytes(raw_data)
            
            # Store metadata
            self._metadata[data_hash] = {
                "path": str(file_path),
                "media_type": media_type,
                "session_id": session_id,
                "source_name": source_name,
                "size_bytes": len(raw_data),
                "created_at": time.time(),
                "last_accessed": time.time()
            }
            self._save_metadata()
            
            logger.info(f"Stored media: {file_path} ({len(raw_data) / 1024:.1f}KB)")
            
            # Trigger cleanup if needed
            self._maybe_cleanup()
            
            return str(file_path)
            
        except Exception as e:
            logger.error(f"Failed to store media: {e}")
            return None
    
    def get_path(self, data_hash: str) -> str | None:
        """Get file path by data hash."""
        if data_hash in self._metadata:
            meta = self._metadata[data_hash]
            path = Path(meta["path"])
            if path.exists():
                meta["last_accessed"] = time.time()
                self._save_metadata()
                return str(path)
        return None
    
    def _maybe_cleanup(self) -> None:
        """Run cleanup if enough time has passed since last cleanup."""
        now = time.time()
        if now - self._last_cleanup < self.cleanup_interval:
            return
        
        self._last_cleanup = now
        self.cleanup()
    
    def cleanup(self) -> int:
        """Remove expired files and enforce max_files limit.
        
        Returns:
            Number of files removed
        """
        removed = 0
        now = time.time()
        
        # First pass: remove expired files
        expired_hashes = []
        for data_hash, meta in list(self._metadata.items()):
            # Idle time, not age: storing the same bytes again refreshes only
            # last_accessed and hands out the existing path in a new hint —
            # expiring by created_at deleted a file that hint had just named.
            last_used = meta.get("last_accessed") or meta.get("created_at", 0)
            if now - last_used > self.ttl_seconds:
                expired_hashes.append(data_hash)
        
        for data_hash in expired_hashes:
            meta = self._metadata.pop(data_hash, None)
            if meta:
                path = Path(meta["path"])
                if path.exists():
                    try:
                        path.unlink()
                        removed += 1
                        logger.debug(f"Removed expired media: {path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove {path}: {e}")
        
        # Second pass: enforce max_files (remove oldest first)
        if len(self._metadata) > self.max_files:
            # Sort by last_accessed time
            sorted_items = sorted(
                self._metadata.items(),
                key=lambda x: x[1].get("last_accessed", 0)
            )
            
            # Remove oldest until under limit
            to_remove = len(self._metadata) - self.max_files
            for data_hash, meta in sorted_items[:to_remove]:
                del self._metadata[data_hash]
                path = Path(meta["path"])
                if path.exists():
                    try:
                        path.unlink()
                        removed += 1
                        logger.debug(f"Removed oldest media: {path}")
                    except Exception as e:
                        logger.warning(f"Failed to remove {path}: {e}")
        
        # Clean up empty session directories
        for session_dir in self.storage_path.iterdir():
            if session_dir.is_dir() and session_dir.name != "metadata.json":
                if not any(session_dir.iterdir()):
                    try:
                        session_dir.rmdir()
                    except Exception:
                        pass
        
        if removed > 0:
            self._save_metadata()
            logger.info(f"MediaStore cleanup: removed {removed} files")
        
        return removed
    
    def get_stats(self) -> dict[str, Any]:
        """Get storage statistics."""
        total_size = 0
        for meta in self._metadata.values():
            total_size += meta.get("size_bytes", 0)
        
        return {
            "file_count": len(self._metadata),
            "total_size_bytes": total_size,
            "total_size_mb": total_size / (1024 * 1024),
            "ttl_seconds": self.ttl_seconds,
            "max_files": self.max_files,
            "storage_path": str(self.storage_path)
        }
