"""Tests for schema-based router generator."""

import pytest
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from fastapi import FastAPI

from agent_system.plugins.schema_router import (
    SchemaRouterGenerator,
    create_schema_router
)


class MockHandlerClass:
    """Mock handler class for testing."""
    
    async def get_home(self, request: Request):
        return HTMLResponse("<h1>Home</h1>")
    
    async def get_data(self, request: Request):
        return JSONResponse({"status": "ok", "data": []})
    
    async def create_item(self, request: Request):
        return JSONResponse({"status": "created"})
    
    async def update_item(self, request: Request, item_id: str):
        return JSONResponse({"status": "updated", "item_id": item_id})
    
    async def delete_item(self, request: Request, item_id: str):
        return JSONResponse({"status": "deleted", "item_id": item_id})


@pytest.fixture
def mock_schema():
    """Minimal schema with endpoint definitions."""
    return {
        "web_ui": {
            "endpoints": [
                {
                    "path": "/",
                    "method": "GET",
                    "handler": "get_home",
                    "response_type": "html",
                    "description": "Home page"
                },
                {
                    "path": "/data",
                    "method": "GET",
                    "handler": "get_data",
                    "response_type": "json",
                    "description": "Get data"
                },
                {
                    "path": "/items",
                    "method": "POST",
                    "handler": "create_item",
                    "response_type": "json",
                    "description": "Create item"
                },
                {
                    "path": "/items/{item_id}",
                    "method": "PUT",
                    "handler": "update_item",
                    "response_type": "json",
                    "description": "Update item"
                },
                {
                    "path": "/items/{item_id}",
                    "method": "DELETE",
                    "handler": "delete_item",
                    "response_type": "json",
                    "description": "Delete item"
                }
            ]
        }
    }


@pytest.fixture
def handler_instance():
    """Handler class instance."""
    return MockHandlerClass()


@pytest.fixture
def app_with_router(mock_schema, handler_instance):
    """FastAPI app with schema-generated router."""
    app = FastAPI()
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance
    )
    router = generator.generate_router()
    app.include_router(router)
    
    return app


def test_schema_router_generator_init(mock_schema, handler_instance):
    """Test SchemaRouterGenerator initialization."""
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance
    )
    
    assert generator.plugin_name == "test_plugin"
    assert generator.prefix == "/plugins/test_plugin"
    assert len(generator.endpoints) == 5


def test_schema_router_generator_custom_prefix(mock_schema, handler_instance):
    """Test custom prefix."""
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance,
        prefix="/custom"
    )
    
    assert generator.prefix == "/custom"


def test_generate_router(mock_schema, handler_instance):
    """Test router generation."""
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance
    )
    
    router = generator.generate_router()
    assert isinstance(router, APIRouter)
    assert len(router.routes) == 5


def test_missing_handler_raises_error(mock_schema, handler_instance):
    """Test that missing handler method raises error."""
    # Add endpoint with non-existent handler
    mock_schema["web_ui"]["endpoints"].append({
        "path": "/missing",
        "method": "GET",
        "handler": "nonexistent_method",
        "response_type": "json"
    })
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance
    )
    
    with pytest.raises(RuntimeError, match="Handler method 'nonexistent_method' not found"):
        generator.generate_router()


def test_missing_handler_name_raises_error(handler_instance):
    """Test that missing 'handler' field raises error."""
    schema = {
        "web_ui": {
            "endpoints": [
                {
                    "path": "/test",
                    "method": "GET",
                    # Missing 'handler' field
                    "response_type": "json"
                }
            ]
        }
    }
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=schema,
        handler_class=handler_instance
    )
    
    with pytest.raises(RuntimeError, match="Missing 'handler' in endpoint definition"):
        generator.generate_router()


def test_unsupported_http_method_raises_error(handler_instance):
    """Test that unsupported HTTP method raises error."""
    schema = {
        "web_ui": {
            "endpoints": [
                {
                    "path": "/test",
                    "method": "INVALID",
                    "handler": "get_home",
                    "response_type": "json"
                }
            ]
        }
    }
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=schema,
        handler_class=handler_instance
    )
    
    with pytest.raises(ValueError, match="Unsupported HTTP method 'INVALID'"):
        generator.generate_router()


def test_empty_endpoints_returns_empty_router(handler_instance):
    """Test that empty endpoints returns router without routes."""
    schema = {
        "web_ui": {
            "endpoints": []
        }
    }
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=schema,
        handler_class=handler_instance
    )
    
    router = generator.generate_router()
    assert len(router.routes) == 0


def test_no_web_ui_section(handler_instance):
    """Test schema without web_ui section."""
    schema = {}
    
    generator = SchemaRouterGenerator(
        plugin_name="test_plugin",
        schema=schema,
        handler_class=handler_instance
    )
    
    router = generator.generate_router()
    assert len(router.routes) == 0


def test_create_schema_router_convenience_function(mock_schema, handler_instance):
    """Test convenience function."""
    router = create_schema_router(
        plugin_name="test_plugin",
        schema=mock_schema,
        handler_class=handler_instance
    )
    
    assert isinstance(router, APIRouter)
    assert len(router.routes) == 5


@pytest.mark.asyncio
async def test_generated_routes_work(app_with_router):
    """Test that generated routes actually work."""
    client = TestClient(app_with_router)
    
    # Test GET /
    response = client.get("/plugins/test_plugin/")
    assert response.status_code == 200
    assert "Home" in response.text
    
    # Test GET /data
    response = client.get("/plugins/test_plugin/data")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "data": []}
    
    # Test POST /items
    response = client.post("/plugins/test_plugin/items")
    assert response.status_code == 200
    assert response.json() == {"status": "created"}
    
    # Test PUT /items/{item_id}
    response = client.put("/plugins/test_plugin/items/123")
    assert response.status_code == 200
    assert response.json() == {"status": "updated", "item_id": "123"}
    
    # Test DELETE /items/{item_id}
    response = client.delete("/plugins/test_plugin/items/456")
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "item_id": "456"}


def test_response_type_mapping():
    """Test response type to class mapping."""
    handler = MockHandlerClass()
    
    schema_html = {
        "web_ui": {
            "endpoints": [{
                "path": "/",
                "method": "GET",
                "handler": "get_home",
                "response_type": "html"
            }]
        }
    }
    
    schema_json = {
        "web_ui": {
            "endpoints": [{
                "path": "/",
                "method": "GET",
                "handler": "get_data",
                "response_type": "json"
            }]
        }
    }
    
    # Test HTML response
    router_html = create_schema_router("test", schema_html, handler)
    assert len(router_html.routes) == 1
    
    # Test JSON response
    router_json = create_schema_router("test", schema_json, handler)
    assert len(router_json.routes) == 1
