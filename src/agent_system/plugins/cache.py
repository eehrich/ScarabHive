"""
Plugin caching system for AgentSystem.

Provides a file-based cache with TTL (Time To Live) support for plugins.
Cache files are stored in .cache/{plugin_name}/ subdirectories.
"""
import json
import hashlib
import time
from pathlib import Path
from typing import Any, Optional, Dict
import logging

logger = logging.getLogger(__name__)


class PluginCache:
    """File-based cache with TTL support for plugins."""
    
    def __init__(self, plugin_name: str, cache_dir: Optional[Path] = None, default_ttl: int = 3600):
        """
        Initialize plugin cache.
        
        Args:
            plugin_name: Name of the plugin (used for cache subdirectory)
            cache_dir: Base cache directory (defaults to .cache in project root)
            default_ttl: Default TTL in seconds (default: 1 hour)
        """
        self.plugin_name = plugin_name
        self.default_ttl = default_ttl
        
        # Set up cache directory
        if cache_dir is None:
            # Find project root by looking for pyproject.toml
            current_path = Path(__file__).resolve()
            project_root = current_path
            while project_root.parent != project_root:
                if (project_root / "pyproject.toml").exists():
                    break
                project_root = project_root.parent
            else:
                # Fallback to current working directory
                project_root = Path.cwd()
            
            cache_dir = project_root / ".cache"
        
        self.cache_dir = cache_dir / plugin_name
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        logger.debug(f"Initialized cache for {plugin_name} at {self.cache_dir}")
    
    def _get_cache_key(self, key: str) -> str:
        """Generate a safe filename from cache key."""
        # Use MD5 hash to ensure safe filenames
        key_hash = hashlib.md5(key.encode('utf-8')).hexdigest()
        return f"{key_hash}.json"
    
    def _get_cache_file(self, key: str) -> Path:
        """Get the cache file path for a key."""
        return self.cache_dir / self._get_cache_key(key)
    
    async def get(self, key: str) -> Optional[Any]:
        """
        Get a value from cache.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value if exists and not expired, None otherwise
        """
        cache_file = self._get_cache_file(key)
        
        if not cache_file.exists():
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Check if cache entry has expired
            if cache_data.get('expires_at', 0) < time.time():
                # Cache expired, remove file
                cache_file.unlink(missing_ok=True)
                logger.debug(f"Cache expired for key: {key[:50]}...")
                return None
            
            logger.debug(f"Cache hit for key: {key[:50]}...")
            return cache_data.get('data')
            
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning(f"Failed to read cache file {cache_file}: {e}")
            # Remove corrupted cache file
            cache_file.unlink(missing_ok=True)
            return None
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Store a value in cache.
        
        Args:
            key: Cache key
            value: Value to cache (must be JSON serializable)
            ttl: Time to live in seconds (uses default_ttl if None)
            
        Returns:
            True if successfully cached, False otherwise
        """
        if ttl is None:
            ttl = self.default_ttl
        
        cache_file = self._get_cache_file(key)
        expires_at = time.time() + ttl
        
        cache_data = {
            'data': value,
            'created_at': time.time(),
            'expires_at': expires_at,
            'ttl': ttl,
            'key_preview': key[:100]  # Store preview for debugging
        }
        
        try:
            # Write atomically by using a temporary file
            temp_file = cache_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)
            
            # Atomic move
            temp_file.replace(cache_file)
            
            logger.debug(f"Cached data for key: {key[:50]}... (TTL: {ttl}s)")
            return True
            
        except (OSError, TypeError) as e:
            logger.error(f"Failed to write cache file {cache_file}: {e}")
            # Cleanup temp file if it exists
            temp_file.unlink(missing_ok=True)
            return False
    
    async def delete(self, key: str) -> bool:
        """
        Delete a cache entry.
        
        Args:
            key: Cache key
            
        Returns:
            True if deleted, False if not found
        """
        cache_file = self._get_cache_file(key)
        
        if cache_file.exists():
            try:
                cache_file.unlink()
                logger.debug(f"Deleted cache for key: {key[:50]}...")
                return True
            except OSError as e:
                logger.error(f"Failed to delete cache file {cache_file}: {e}")
                return False
        
        return False
    
    async def clear(self) -> int:
        """
        Clear all cache entries for this plugin.
        
        Returns:
            Number of files deleted
        """
        deleted_count = 0
        
        try:
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    cache_file.unlink()
                    deleted_count += 1
                except OSError as e:
                    logger.error(f"Failed to delete cache file {cache_file}: {e}")
            
            logger.info(f"Cleared {deleted_count} cache files for {self.plugin_name}")
            return deleted_count
            
        except OSError as e:
            logger.error(f"Failed to clear cache directory {self.cache_dir}: {e}")
            return deleted_count
    
    async def cleanup_expired(self) -> int:
        """
        Remove expired cache entries.
        
        Returns:
            Number of expired files deleted
        """
        deleted_count = 0
        current_time = time.time()
        
        try:
            for cache_file in self.cache_dir.glob("*.json"):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                    
                    if cache_data.get('expires_at', 0) < current_time:
                        cache_file.unlink()
                        deleted_count += 1
                        
                except (json.JSONDecodeError, KeyError, OSError) as e:
                    logger.warning(f"Failed to process cache file {cache_file}: {e}")
                    # Remove corrupted files
                    cache_file.unlink(missing_ok=True)
                    deleted_count += 1
            
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} expired cache files for {self.plugin_name}")
            
            return deleted_count
            
        except OSError as e:
            logger.error(f"Failed to cleanup cache directory {self.cache_dir}: {e}")
            return deleted_count
    
    def get_cache_info(self) -> Dict[str, Any]:
        """
        Get cache statistics and information.
        
        Returns:
            Dictionary with cache statistics
        """
        try:
            cache_files = list(self.cache_dir.glob("*.json"))
            total_files = len(cache_files)
            
            if total_files == 0:
                return {
                    'plugin_name': self.plugin_name,
                    'cache_dir': str(self.cache_dir),
                    'total_files': 0,
                    'expired_files': 0,
                    'valid_files': 0,
                    'total_size_mb': 0
                }
            
            expired_count = 0
            total_size = 0
            current_time = time.time()
            
            for cache_file in cache_files:
                try:
                    # Get file size
                    total_size += cache_file.stat().st_size
                    
                    # Check if expired
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cache_data = json.load(f)
                    
                    if cache_data.get('expires_at', 0) < current_time:
                        expired_count += 1
                        
                except (json.JSONDecodeError, KeyError, OSError):
                    expired_count += 1  # Count corrupted files as expired
            
            return {
                'plugin_name': self.plugin_name,
                'cache_dir': str(self.cache_dir),
                'total_files': total_files,
                'expired_files': expired_count,
                'valid_files': total_files - expired_count,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'default_ttl': self.default_ttl
            }
            
        except OSError as e:
            logger.error(f"Failed to get cache info for {self.cache_dir}: {e}")
            return {
                'plugin_name': self.plugin_name,
                'cache_dir': str(self.cache_dir),
                'error': str(e)
            }


def create_cache_key(*args, **kwargs) -> str:
    """
    Create a cache key from arguments.
    
    Args:
        *args: Positional arguments
        **kwargs: Keyword arguments
        
    Returns:
        String cache key
    """
    # Sort kwargs for consistent key generation
    sorted_kwargs = sorted(kwargs.items()) if kwargs else []
    
    # Combine args and kwargs into a tuple for hashing
    key_data = {
        'args': args,
        'kwargs': sorted_kwargs
    }
    
    # Convert to JSON string for consistent representation
    return json.dumps(key_data, sort_keys=True, separators=(',', ':'))