# Schema-Based Web Routing

**Status:** ✅ Implemented  
**Version:** 1.0.0  
**Date:** 2025-11-14

## Overview

The schema-based web routing system automatically generates FastAPI routes from `schema.yaml` endpoint definitions, eliminating code duplication and ensuring consistency between documentation and implementation.

## Motivation

**Problem:**  
Previously, plugins defined their web endpoints twice:
1. In `schema.yaml` (documentation only)
2. In Python code with `@router.get()` decorators (actual implementation)

This led to:
- Duplication and maintenance burden
- Drift between documentation and implementation
- Verbose boilerplate code in every plugin

**Solution:**  
Define endpoints once in `schema.yaml`, let the system generate the routes automatically.

## Architecture

```
schema.yaml
    ├─> SchemaRouterGenerator
    │       ├─> Reads endpoint definitions
    │       ├─> Validates handler methods exist
    │       └─> Generates FastAPI routes
    └─> Handler Class
            └─> Simple async methods (no decorators)
```

### Components

1. **`SchemaRouterGenerator`** (`src/agent_system/plugins/schema_router.py`)
   - Core routing generator
   - Validates schema structure
   - Maps HTTP methods to FastAPI decorators
   - Connects routes to handler methods

2. **Schema Definition** (`schema.yaml`)
   - Single source of truth for endpoints
   - Already supports Jinja2 templates (`{{ name }}`)

3. **Handler Class** (Plugin web endpoints)
   - Simple async methods
   - No decorator boilerplate
   - Clean separation of concerns

## Usage

### 1. Define Endpoints in Schema

```yaml
# schema.yaml
web_ui:
  panel:
    enabled: true
    title: "My Plugin"
    endpoint: "/plugins/{{ name }}/"
  
  endpoints:
    - path: "/"
      method: "GET"
      handler: "get_dashboard"
      response_type: "html"
      description: "Main dashboard"
    
    - path: "/api/data"
      method: "GET"
      handler: "get_data"
      response_type: "json"
      description: "Get data as JSON"
    
    - path: "/api/items"
      method: "POST"
      handler: "create_item"
      response_type: "json"
      description: "Create new item"
    
    - path: "/api/items/{item_id}"
      method: "PUT"
      handler: "update_item"
      response_type: "json"
      description: "Update existing item"
    
    - path: "/api/items/{item_id}"
      method: "DELETE"
      handler: "delete_item"
      response_type: "json"
      description: "Delete item"
```

### 2. Create Handler Methods

```python
# web_endpoints.py
from fastapi import Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from agent_system.plugins.schema_router import create_schema_router

class MyPluginWebFactory:
    def __init__(self, server):
        self.server = server
        # ... setup templates, etc
    
    def get_web_router(self):
        """Generate router from schema."""
        schema = self.server.get_schema_data()
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )
    
    # Handler methods (called by generated routes)
    
    async def get_dashboard(self, request: Request) -> HTMLResponse:
        """Handler for GET /"""
        return self.templates.TemplateResponse("dashboard.html", {
            "request": request
        })
    
    async def get_data(self, request: Request) -> JSONResponse:
        """Handler for GET /api/data"""
        data = await self._fetch_data()
        return JSONResponse({"status": "ok", "data": data})
    
    async def create_item(
        self,
        request: Request,
        name: str = Query(...),
        value: int = Query(...)
    ) -> JSONResponse:
        """Handler for POST /api/items"""
        item_id = await self._create_item(name, value)
        return JSONResponse({"status": "created", "item_id": item_id})
    
    async def update_item(
        self,
        request: Request,
        item_id: str,  # Path parameter
        name: str = Query(...)
    ) -> JSONResponse:
        """Handler for PUT /api/items/{item_id}"""
        await self._update_item(item_id, name)
        return JSONResponse({"status": "updated", "item_id": item_id})
    
    async def delete_item(
        self,
        request: Request,
        item_id: str  # Path parameter
    ) -> JSONResponse:
        """Handler for DELETE /api/items/{item_id}"""
        await self._delete_item(item_id)
        return JSONResponse({"status": "deleted", "item_id": item_id})
```

### 3. That's It!

No manual route registration, no `@router.get()` decorators, no duplication.

## Schema Format

### Endpoint Definition

```yaml
endpoints:
  - path: "/path/{param}"        # Route path (supports FastAPI path params)
    method: "GET"                  # HTTP method: GET, POST, PUT, DELETE, PATCH
    handler: "handler_method_name" # Method name in handler class (required)
    response_type: "json"          # Response type: json, html, text, response
    description: "Endpoint docs"   # Optional description
```

### Response Types

| Type | FastAPI Class | Use Case |
|------|--------------|----------|
| `json` | `JSONResponse` | API endpoints returning JSON |
| `html` | `HTMLResponse` | Web pages/templates |
| `text` | `Response` | Plain text responses |
| `response` | `Response` | Custom/binary responses |

### HTTP Methods

Supported: `GET`, `POST`, `PUT`, `DELETE`, `PATCH`

## Handler Method Signature

### Required Parameters

```python
async def handler_name(
    self,
    request: Request,  # Always first parameter
    # ... other parameters
) -> ResponseType:
```

### Path Parameters

Extracted automatically from URL:

```yaml
path: "/items/{item_id}/details/{detail_id}"
```

```python
async def get_item_detail(
    self,
    request: Request,
    item_id: str,      # From path
    detail_id: str     # From path
) -> JSONResponse:
```

### Query Parameters

Use FastAPI `Query`:

```python
from fastapi import Query

async def search_items(
    self,
    request: Request,
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, description="Max results")
) -> JSONResponse:
```

### Request Body

Use Pydantic models:

```python
from pydantic import BaseModel

class CreateItemRequest(BaseModel):
    name: str
    value: int

async def create_item(
    self,
    request: Request,
    data: CreateItemRequest
) -> JSONResponse:
```

## Error Handling

### Missing Handler Method

```python
RuntimeError: Handler method 'nonexistent_handler' not found in MyWebFactory
for endpoint POST /api/items. Add method: async def nonexistent_handler(self, request: Request) -> Response
```

### Missing Handler Field

```python
RuntimeError: Missing 'handler' in endpoint definition for /api/test.
Add 'handler: method_name' to endpoint in schema.yaml
```

### Invalid HTTP Method

```python
ValueError: Unsupported HTTP method 'INVALID'. Supported methods: ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']
```

## Validation

The `SchemaRouterGenerator` validates at initialization:

1. ✅ Handler method exists in handler class
2. ✅ HTTP method is supported
3. ✅ `handler` field is present in schema
4. ✅ Response type is valid

Validation happens when `generate_router()` is called, before the server starts.

## Comparison: Before vs After

### Before (Manual Routing)

```python
# schema.yaml - Documentation only, not used
endpoints:
  - path: "/data"
    method: "GET"
    description: "Get data"

# web_endpoints.py - Actual implementation
def get_web_router(self):
    router = APIRouter(prefix=f"/plugins/{self.name}")
    
    @router.get("/data", response_class=JSONResponse)
    async def get_data(request: Request):
        data = await self._fetch_data()
        return JSONResponse({"data": data})
    
    return router
```

**Issues:**
- Endpoint defined twice (schema + code)
- Boilerplate: `@router.get`, `async def`, return statement
- Schema not enforced, can drift
- Hard to maintain consistency

### After (Schema-Based Routing)

```yaml
# schema.yaml - Single source of truth
endpoints:
  - path: "/data"
    method: "GET"
    handler: "get_data"
    response_type: "json"
    description: "Get data"
```

```python
# web_endpoints.py - Clean handler
def get_web_router(self):
    return create_schema_router(
        plugin_name=self.server.name,
        schema=self.server.get_schema_data(),
        handler_class=self
    )

async def get_data(self, request: Request) -> JSONResponse:
    data = await self._fetch_data()
    return JSONResponse({"data": data})
```

**Benefits:**
- ✅ Single source of truth (schema.yaml)
- ✅ No boilerplate decorators
- ✅ Enforced consistency
- ✅ Easy to maintain
- ✅ Auto-validated

## Testing

```python
# test_my_plugin_routes.py
from fastapi.testclient import TestClient
from fastapi import FastAPI

def test_schema_generated_routes():
    app = FastAPI()
    plugin = MyPlugin(...)
    router = plugin.get_web_router()
    app.include_router(router)
    
    client = TestClient(app)
    
    # Test generated routes work
    response = client.get("/plugins/my_plugin/")
    assert response.status_code == 200
    
    response = client.get("/plugins/my_plugin/api/data")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

See `tests/test_schema_router.py` for comprehensive test suite.

## Migration Guide

### Step 1: Add Endpoints to Schema

```yaml
web_ui:
  endpoints:
    - path: "/"
      method: "GET"
      handler: "get_dashboard"
      response_type: "html"
```

### Step 2: Update get_web_router()

```python
# Before
def get_web_router(self):
    router = APIRouter(prefix=f"/plugins/{self.name}")
    
    @router.get("/", response_class=HTMLResponse)
    async def get_dashboard(request: Request):
        return self.render_dashboard(request)
    
    return router

# After
def get_web_router(self):
    return create_schema_router(
        plugin_name=self.server.name,
        schema=self.server.get_schema_data(),
        handler_class=self
    )
```

### Step 3: Convert Routes to Methods

```python
# Remove decorator, make it a class method
async def get_dashboard(self, request: Request) -> HTMLResponse:
    return self.render_dashboard(request)
```

### Step 4: Test

```bash
python -m pytest tests/test_my_plugin.py -v
```

## Examples

### Example 1: sub_agent_manager

**Schema:** `src/plugins/sub_agent_manager/schema.yaml`
```yaml
web_ui:
  endpoints:
    - path: "/"
      method: "GET"
      handler: "get_panel"
      response_type: "html"
    
    - path: "/sub-agents"
      method: "GET"
      handler: "get_sub_agents_json"
      response_type: "json"
    
    - path: "/sub-agents/{agent_id}"
      method: "GET"
      handler: "get_sub_agent_detail"
      response_type: "json"
    
    - path: "/sub-agents/{agent_id}"
      method: "DELETE"
      handler: "delete_sub_agent"
      response_type: "json"
```

**Code:** `src/plugins/sub_agent_manager/web_endpoints.py`
```python
class SubAgentManagerWebFactory:
    def get_web_router(self) -> APIRouter:
        schema = self.server.get_schema_data()
        return create_schema_router(
            plugin_name=self.server.name,
            schema=schema,
            handler_class=self
        )
    
    async def get_panel(self, request: Request) -> HTMLResponse:
        return self.render_panel(request)
    
    async def get_sub_agents_json(
        self,
        request: Request,
        session_id: str = Query(...),
        include_completed: bool = Query(False)
    ) -> JSONResponse:
        # Implementation
        pass
```

## Best Practices

### 1. Handler Naming

Use descriptive, action-oriented names:
- ✅ `get_dashboard`, `create_item`, `update_user`
- ❌ `handler1`, `endpoint_2`, `do_stuff`

### 2. Response Types

Match response type to content:
- API endpoints → `json`
- Web pages → `html`
- Downloads → `response`

### 3. Path Parameters

Keep them simple and semantic:
- ✅ `/users/{user_id}/posts/{post_id}`
- ❌ `/users/{id1}/posts/{id2}`

### 4. Documentation

Add meaningful descriptions:
```yaml
- path: "/users/{user_id}"
  method: "GET"
  handler: "get_user"
  response_type: "json"
  description: "Retrieve user profile by ID"  # ✅ Clear purpose
```

### 5. Error Handling

Handle errors in handler methods:
```python
async def get_user(self, request: Request, user_id: str) -> JSONResponse:
    try:
        user = await self._fetch_user(user_id)
        if not user:
            raise HTTPException(404, "User not found")
        return JSONResponse(user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user: {e}")
        raise HTTPException(500, str(e))
```

## Limitations

1. **Dynamic Routes:** Complex dynamic routing patterns may need manual implementation
2. **Middleware:** Per-route middleware must be added in handler methods
3. **Dependencies:** FastAPI dependencies work in handler method signatures

## See Also

- [Schema-Based MCP Server](../mcp_streamable_http_transport.md)
- [Plugin Authoring Guide](plugin_authoring.md)
- [API Reference](../README.md)
- Tests: `tests/test_schema_router.py`
- Example: `src/plugins/sub_agent_manager/`

## Future Enhancements

- [ ] Support for WebSocket endpoints
- [ ] Automatic OpenAPI documentation generation
- [ ] Per-route dependency injection from schema
- [ ] Request validation schemas in YAML
- [ ] Response schema validation

## Changelog

### v1.0.0 (2025-11-14)
- ✅ Initial implementation
- ✅ Support for GET, POST, PUT, DELETE, PATCH
- ✅ Path and query parameters
- ✅ Response type mapping
- ✅ Comprehensive test suite
- ✅ sub_agent_manager migration complete
