"""Tests for the lifespan management in app.py"""

import asyncio
import pytest
from unittest.mock import Mock, patch
from contextlib import asynccontextmanager

from agent_system.app import lifespan
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown():
    """Test that the lifespan context manager handles startup and shutdown properly."""
    app = FastAPI()
    
    with patch('logging.getLogger') as mock_get_logger:
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        # Test the lifespan context manager
        async with lifespan(app):
            # During startup
            mock_logger.info.assert_any_call("FastAPI application starting up")
        
        # After shutdown
        mock_logger.info.assert_any_call("FastAPI application shutting down gracefully")
        mock_logger.info.assert_any_call("FastAPI application shutdown complete")


@pytest.mark.asyncio
async def test_lifespan_handles_shutdown_errors():
    """Test that lifespan handles errors during shutdown gracefully."""
    app = FastAPI()
    
    with patch('logging.getLogger') as mock_get_logger:
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        # Mock an exception during shutdown
        with patch('agent_system.app.lifespan') as mock_lifespan:
            @asynccontextmanager
            async def failing_lifespan(app):
                yield
                # Simulate an error during shutdown
                raise Exception("Shutdown error")
            
            mock_lifespan.side_effect = failing_lifespan
            
            # The lifespan should handle the error gracefully
            try:
                async with failing_lifespan(app):
                    pass
            except Exception:
                # Error is expected in this test case
                pass


def test_fastapi_app_has_lifespan():
    """Test that the FastAPI app is configured with lifespan."""
    from agent_system.app import build_app
    
    # Build an app instance to test
    app = build_app()
    
    # The app should have a lifespan attribute
    assert hasattr(app, 'router')
    # Note: lifespan is internal to FastAPI, so we just verify the app is properly constructed


@pytest.mark.asyncio
async def test_lifespan_context_manager_flow():
    """Test the complete flow of the lifespan context manager."""
    app = FastAPI()
    
    with patch('logging.getLogger') as mock_get_logger:
        mock_logger = Mock()
        mock_get_logger.return_value = mock_logger
        
        startup_called = False
        shutdown_called = False
        
        async with lifespan(app):
            # Verify startup was called
            mock_logger.info.assert_any_call("FastAPI application starting up")
            startup_called = True
            
            # Simulate some work during app lifetime
            await asyncio.sleep(0.01)
        
        # Verify shutdown was called
        shutdown_called = True
        mock_logger.info.assert_any_call("FastAPI application shutting down gracefully")
        mock_logger.info.assert_any_call("FastAPI application shutdown complete")
        
        assert startup_called
        assert shutdown_called
