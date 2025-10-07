"""
Tool caching for MCP integration to avoid re-initializing tools on every request.

This module provides a configuration-aware cache that invalidates automatically
when the MCP configuration changes (detected via config hash).
"""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheStatistics:
    """Statistics for cache performance monitoring"""
    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    sets: int = 0

    def to_dict(self) -> Dict[str, int]:
        """Convert to dictionary for JSON serialization"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "sets": self.sets,
            "hit_rate": self.hit_rate
        }

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate as percentage"""
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return round((self.hits / total) * 100, 2)


class ToolCache:
    """
    Configuration-aware cache for MCP tools.
    
    Automatically invalidates when configuration changes are detected via config hash.
    Thread-safe for concurrent access.
    """

    def __init__(self, enabled: bool = True, max_size: Optional[int] = None):
        """
        Initialize tool cache.
        
        Args:
            enabled: Whether caching is enabled
            max_size: Maximum number of cache entries (None = unlimited)
        """
        self.enabled = enabled
        self.max_size = max_size
        self._cache: Dict[str, Any] = {}
        self._config_hash: Optional[str] = None
        self._lock = asyncio.Lock()
        self._stats = CacheStatistics()
        
        logger.info(f"ToolCache initialized (enabled={enabled}, max_size={max_size})")

    async def get(self, key: str, config_hash: str) -> Optional[Any]:
        """
        Get cached value if config hash matches.
        
        Args:
            key: Cache key
            config_hash: Current configuration hash
            
        Returns:
            Cached value or None if not found/invalidated
        """
        if not self.enabled:
            return None

        async with self._lock:
            # Check if config changed
            if self._config_hash != config_hash:
                logger.debug(f"Config hash mismatch: cache={self._config_hash}, current={config_hash}")
                if len(self._cache) > 0:  # Only count as invalidation if cache had entries
                    await self._invalidate_all()
                self._config_hash = config_hash
                self._stats.misses += 1  # Config mismatch counts as miss
                return None

            value = self._cache.get(key)
            if value is not None:
                self._stats.hits += 1
                logger.debug(f"Cache HIT: key={key} (hits={self._stats.hits})")
                return value
            else:
                self._stats.misses += 1
                logger.debug(f"Cache MISS: key={key} (misses={self._stats.misses})")
                return None

    async def set(self, key: str, value: Any, config_hash: str) -> None:
        """
        Set cached value with config hash.
        
        Args:
            key: Cache key
            value: Value to cache
            config_hash: Current configuration hash
        """
        if not self.enabled:
            return

        async with self._lock:
            # Update config hash if it changed
            if self._config_hash != config_hash:
                logger.debug("Config changed during set, invalidating cache")
                if len(self._cache) > 0:  # Only count as invalidation if cache had entries
                    await self._invalidate_all()
                else:
                    self._cache.clear()  # Clear without counting
                self._config_hash = config_hash

            # Enforce max size with simple FIFO eviction
            if self.max_size and len(self._cache) >= self.max_size:
                # Remove oldest entry (first key)
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                logger.debug(f"Cache full, evicted key={oldest_key}")

            self._cache[key] = value
            self._stats.sets += 1
            logger.debug(f"Cache SET: key={key} (size={len(self._cache)})")

    async def invalidate(self, key: Optional[str] = None) -> None:
        """
        Invalidate cache entry or entire cache.
        
        Args:
            key: Specific key to invalidate, or None for entire cache
        """
        async with self._lock:
            if key is None:
                await self._invalidate_all()
            else:
                if key in self._cache:
                    del self._cache[key]
                    self._stats.invalidations += 1
                    logger.debug(f"Cache invalidated: key={key}")

    async def _invalidate_all(self) -> None:
        """Invalidate entire cache (internal, requires lock)"""
        count = len(self._cache)
        self._cache.clear()
        self._stats.invalidations += 1
        logger.debug(f"Cache invalidated: cleared {count} entries")

    async def get_statistics(self) -> Dict[str, Any]:
        """Get cache statistics"""
        async with self._lock:
            return {
                "enabled": self.enabled,
                "size": len(self._cache),
                "max_size": self.max_size,
                "config_hash": self._config_hash,
                "statistics": self._stats.to_dict()
            }

    def compute_config_hash(self, config_data: Any) -> str:
        """
        Compute hash of configuration for cache invalidation.
        
        Args:
            config_data: Configuration data (will be JSON serialized)
            
        Returns:
            SHA256 hash of configuration
        """
        try:
            config_json = json.dumps(config_data, sort_keys=True, default=str)
            return hashlib.sha256(config_json.encode()).hexdigest()[:16]
        except Exception as e:
            logger.warning(f"Failed to compute config hash: {e}")
            # Return random hash to force cache miss
            import time
            return hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
