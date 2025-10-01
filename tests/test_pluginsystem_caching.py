"""
Tests for plugin cache system functionality.
"""
import pytest
import asyncio
from unittest.mock import patch
from agent_system.plugins.cache import PluginCache, create_cache_key


class TestPluginCache:
    """Test the PluginCache class functionality."""

    @pytest.fixture
    def cache_dir(self, tmp_path):
        """Create a temporary cache directory."""
        return tmp_path / "test_cache"

    @pytest.fixture
    def cache(self, cache_dir):
        """Create a PluginCache instance for testing."""
        return PluginCache(plugin_name="test_plugin", cache_dir=cache_dir.parent, default_ttl=60)

    @pytest.mark.asyncio
    async def test_cache_initialization(self, cache_dir):
        """Test cache initialization and directory creation."""
        cache = PluginCache(plugin_name="test_plugin", cache_dir=cache_dir.parent, default_ttl=60)
        
        assert cache.plugin_name == "test_plugin"
        assert cache.default_ttl == 60
        assert cache.cache_dir.exists()
        assert cache.cache_dir.name == "test_plugin"

    @pytest.mark.asyncio
    async def test_cache_set_and_get(self, cache):
        """Test basic cache set and get operations."""
        key = "test_key"
        value = {"data": "test_value", "number": 42}
        
        # Set cache entry
        result = await cache.set(key, value, ttl=30)
        assert result is True
        
        # Get cache entry
        cached_value = await cache.get(key)
        assert cached_value == value

    @pytest.mark.asyncio
    async def test_cache_expiration(self, cache):
        """Test cache TTL expiration."""
        key = "expiry_test"
        value = {"expires": "soon"}
        
        # Set cache entry with 1 second TTL
        await cache.set(key, value, ttl=1)
        
        # Should be available immediately
        cached_value = await cache.get(key)
        assert cached_value == value
        
        # Wait for expiration
        await asyncio.sleep(1.1)
        
        # Should be None after expiration
        expired_value = await cache.get(key)
        assert expired_value is None

    @pytest.mark.asyncio
    async def test_cache_delete(self, cache):
        """Test cache entry deletion."""
        key = "delete_test"
        value = {"to_be": "deleted"}
        
        # Set and verify
        await cache.set(key, value)
        assert await cache.get(key) == value
        
        # Delete and verify
        result = await cache.delete(key)
        assert result is True
        assert await cache.get(key) is None
        
        # Delete non-existent key
        result = await cache.delete("non_existent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cache_clear(self, cache):
        """Test clearing all cache entries."""
        # Add multiple cache entries
        for i in range(5):
            await cache.set(f"key_{i}", {"value": i})
        
        # Clear all entries
        deleted_count = await cache.clear()
        assert deleted_count == 5
        
        # Verify all entries are gone
        for i in range(5):
            assert await cache.get(f"key_{i}") is None

    @pytest.mark.asyncio
    async def test_cache_cleanup_expired(self, cache):
        """Test cleanup of expired entries."""
        # Add some entries with different TTLs
        await cache.set("short_lived", {"ttl": 1}, ttl=1)
        await cache.set("long_lived", {"ttl": 60}, ttl=60)
        
        # Wait for short-lived entry to expire
        await asyncio.sleep(1.1)
        
        # Cleanup expired entries
        deleted_count = await cache.cleanup_expired()
        assert deleted_count == 1
        
        # Verify correct entries remain
        assert await cache.get("short_lived") is None
        assert await cache.get("long_lived") is not None

    @pytest.mark.asyncio
    async def test_cache_info(self, cache):
        """Test cache statistics and information."""
        # Add some test entries
        await cache.set("info_test_1", {"data": 1})
        await cache.set("info_test_2", {"data": 2})
        
        cache_info = cache.get_cache_info()
        
        assert cache_info["plugin_name"] == "test_plugin"
        assert cache_info["total_files"] == 2
        assert cache_info["valid_files"] == 2
        assert cache_info["expired_files"] == 0
        assert cache_info["default_ttl"] == 60
        assert "total_size_mb" in cache_info

    @pytest.mark.asyncio
    async def test_cache_key_normalization(self, cache):
        """Test cache key generation and normalization."""
        # Test that different key strings produce different cache files
        key1 = "test_key_1"
        key2 = "test_key_2"
        
        await cache.set(key1, {"key": 1})
        await cache.set(key2, {"key": 2})
        
        assert await cache.get(key1) == {"key": 1}
        assert await cache.get(key2) == {"key": 2}

    @pytest.mark.asyncio
    async def test_cache_file_corruption_handling(self, cache):
        """Test handling of corrupted cache files."""
        key = "corruption_test"
        
        # Create a corrupted cache file
        cache_file = cache._get_cache_file(key)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(cache_file, 'w') as f:
            f.write("invalid json content")
        
        # Should return None for corrupted file and remove it
        result = await cache.get(key)
        assert result is None
        assert not cache_file.exists()

    def test_create_cache_key_consistency(self):
        """Test cache key creation consistency."""
        # Same parameters should produce same key
        key1 = create_cache_key("arg1", "arg2", param1="value1", param2="value2")
        key2 = create_cache_key("arg1", "arg2", param2="value2", param1="value1")
        
        assert key1 == key2
        
        # Different parameters should produce different keys
        key3 = create_cache_key("arg1", "arg2", param1="value1", param2="value3")
        
        assert key1 != key3

    @pytest.mark.asyncio
    async def test_atomic_cache_writes(self, cache):
        """Test that cache writes are atomic."""
        key = "atomic_test"
        value = {"atomic": True}
        
        # Mock a failure during write to test cleanup
        with patch('builtins.open', side_effect=OSError("Write failed")):
            result = await cache.set(key, value)
            assert result is False
        
        # Verify no partial files left behind
        cache_file = cache._get_cache_file(key)
        temp_file = cache_file.with_suffix('.tmp')
        
        assert not cache_file.exists()
        assert not temp_file.exists()


class TestWebScraperCaching:
    """Test caching functionality for web scraper plugin."""

    @pytest.fixture
    def mock_scraper(self, tmp_path):
        """Create a mock web scraper with cache enabled."""
        from unittest.mock import Mock
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        from plugins.web_scraper.server import WebScraperServer
        
        system_config = Mock(spec=AgentSystemConfig)
        mcp_config = MCPConfig(
            type="web_scraper",
            enabled=True,
            cache_enabled=True,
            cache_ttl=60
        )
        
        server = WebScraperServer("test_scraper", system_config, mcp_config)
        # Override cache to use temp directory
        server.cache = PluginCache("test_scraper", tmp_path, default_ttl=60)
        
        return server

    def test_cache_key_creation(self, mock_scraper):
        """Test web scraper cache key creation."""
        url = "https://example.com/test"
        
        key1 = mock_scraper._create_cache_key(
            url, "content", 8000, True, False, False, False
        )
        
        key2 = mock_scraper._create_cache_key(
            url, "content", 8000, True, False, False, False
        )
        
        # Same parameters should create same key
        assert key1 == key2
        
        # Different parameters should create different key
        key3 = mock_scraper._create_cache_key(
            url, "links", 8000, True, False, False, False
        )
        
        assert key1 != key3

    def test_url_normalization_in_cache_key(self, mock_scraper):
        """Test URL normalization for consistent cache keys."""
        # URLs with same content but different order of query params
        url1 = "https://example.com?param1=value1&param2=value2"
        url2 = "https://example.com?param2=value2&param1=value1"
        
        key1 = mock_scraper._create_cache_key(url1, "content", 8000, False, False, False, False)
        key2 = mock_scraper._create_cache_key(url2, "content", 8000, False, False, False, False)
        
        # Should create the same cache key due to normalization
        assert key1 == key2


class TestDuckDuckGoSearchCaching:
    """Test caching functionality for DuckDuckGo search plugin."""

    @pytest.fixture
    def mock_ddg_search(self, tmp_path):
        """Create a mock DuckDuckGo search server with cache enabled."""
        from unittest.mock import Mock
        from agent_system.config.models import AgentSystemConfig, MCPConfig
        from plugins.duckduckgo_search.server import DuckDuckGoSearchServer
        
        system_config = Mock(spec=AgentSystemConfig)
        mcp_config = MCPConfig(
            type="duckduckgo_search",
            enabled=True,
            cache_enabled=True,
            cache_ttl=900
        )
        
        server = DuckDuckGoSearchServer("test_ddg", system_config, mcp_config)
        # Override cache to use temp directory
        server.cache = PluginCache("test_ddg", tmp_path, default_ttl=900)
        
        return server

    def test_search_cache_key_creation(self, mock_ddg_search):
        """Test search cache key creation."""
        query = "artificial intelligence"
        max_results = 10
        
        key1 = mock_ddg_search._create_cache_key(query, max_results)
        key2 = mock_ddg_search._create_cache_key(query, max_results)
        
        # Same parameters should create same key
        assert key1 == key2
        
        # Different parameters should create different key
        key3 = mock_ddg_search._create_cache_key("different query", max_results)
        assert key1 != key3
        
        key4 = mock_ddg_search._create_cache_key(query, 5)
        assert key1 != key4

    def test_query_normalization(self, mock_ddg_search):
        """Test query normalization for consistent cache keys."""
        # Queries with different case should normalize to same cache key
        key1 = mock_ddg_search._create_cache_key("AI Machine Learning", 5)
        key2 = mock_ddg_search._create_cache_key("ai machine learning", 5)
        
        # Should be the same due to lowercase normalization
        assert key1 == key2
        
        # Test whitespace normalization
        key3 = mock_ddg_search._create_cache_key("  ai machine learning  ", 5)
        assert key1 == key3


class TestCacheIntegration:
    """Test cache system integration with actual plugin operations."""
    
    @pytest.mark.asyncio
    async def test_cache_functionality(self, tmp_path):
        """Test basic cache functionality with realistic data."""
        cache = PluginCache("performance_test", tmp_path, default_ttl=60)
        
        # Simulate realistic plugin result
        plugin_result = {"computed": "expensive_data", "numbers": list(range(100))}
        cache_key = "functionality_test_key"
        
        # First access - cache miss (store result)
        await cache.set(cache_key, plugin_result)
        
        # Second access - cache hit
        cached_result = await cache.get(cache_key)
        
        # Verify result correctness
        assert cached_result == plugin_result
        
        # Verify cache info shows the entry
        info = cache.get_cache_info()
        assert info['total_files'] == 1
        assert info['valid_files'] == 1

    @pytest.mark.asyncio  
    async def test_concurrent_cache_access(self, tmp_path):
        """Test concurrent access to cache system."""
        cache = PluginCache("concurrent_test", tmp_path, default_ttl=60)
        
        async def cache_operation(operation_id: int):
            """Simulate concurrent cache operations."""
            key = f"concurrent_key_{operation_id}"
            value = {"operation_id": operation_id, "data": f"data_{operation_id}"}
            
            # Set value
            await cache.set(key, value)
            
            # Get value
            result = await cache.get(key)
            assert result == value
            
            return result
        
        # Run multiple concurrent cache operations
        tasks = [cache_operation(i) for i in range(10)]
        results = await asyncio.gather(*tasks)
        
        # Verify all operations completed successfully
        assert len(results) == 10
        for i, result in enumerate(results):
            assert result["operation_id"] == i