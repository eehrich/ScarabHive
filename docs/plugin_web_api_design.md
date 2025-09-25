# Plugin Web Endpoints & Panels API Design

## Overview

This design extends the existing AgentSystem plugin architecture to support web endpoints and UI panels, enabling plugins to provide rich interactive interfaces beyond just MCP tools.

## Architecture Components

### 1. Plugin Base Interface Extension

```python
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, Response
from pathlib import Path

class PluginWebInterface:
    """Base interface for plugins with web capabilities"""
    
    def get_web_router(self) -> Optional[APIRouter]:
        """Return FastAPI router with plugin endpoints"""
        return None
    
    def get_static_assets(self) -> Optional[Path]:
        """Return path to plugin static assets (JS/CSS/HTML)"""
        return None
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return list of UI panel configurations"""
        return []
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration for endpoints"""
        return {
            "require_auth": False,
            "cors_origins": [],
            "rate_limit": None,
            "content_security_policy": None
        }
```

### 2. Plugin Registration Enhancement

Extend the existing `PLUGIN_FACTORY` pattern:

```python
# In plugin.py
class MyPluginServer(PluginWebInterface):
    def __init__(self, name: str, config: Dict[str, Any], ssl_verify: bool = True):
        self.name = name
        self.config = config
        self.ssl_verify = ssl_verify
        self._router = None
    
    def get_web_router(self) -> Optional[APIRouter]:
        if self._router is None:
            self._router = APIRouter(prefix=f"/plugins/{self.name}")
            
            @self._router.get("/status")
            async def plugin_status():
                return {"status": "ok", "plugin": self.name}
            
            @self._router.get("/data")
            async def get_data():
                return await self.fetch_plugin_data()
        
        return self._router
    
    def get_panels(self) -> List[Dict[str, Any]]:
        return [{
            "id": f"{self.name}_panel",
            "title": "My Plugin Panel", 
            "url": f"/plugins/{self.name}/panel.html",
            "icon": "chart-bar",
            "position": "right",
            "width": "400px",
            "height": "300px"
        }]

PLUGIN_FACTORY = MyPluginServer
```

### 3. Backend Integration Points

#### A. Enhanced Plugin Registry

```python
# In src/agent_system/plugins/web_adapter.py
class PluginWebRegistry:
    """Registry for plugin web capabilities"""
    
    def __init__(self):
        self.web_plugins: Dict[str, PluginWebInterface] = {}
        self.active_routers: Dict[str, APIRouter] = {}
        self.static_mounts: Dict[str, Path] = {}
    
    def register_web_plugin(self, name: str, plugin: PluginWebInterface) -> None:
        """Register a plugin's web capabilities"""
        self.web_plugins[name] = plugin
        
        # Register router if provided
        router = plugin.get_web_router()
        if router:
            self.active_routers[name] = router
        
        # Register static assets if provided  
        static_path = plugin.get_static_assets()
        if static_path and static_path.exists():
            self.static_mounts[name] = static_path
    
    def get_all_panels(self) -> List[Dict[str, Any]]:
        """Get all registered UI panels"""
        panels = []
        for name, plugin in self.web_plugins.items():
            plugin_panels = plugin.get_panels()
            for panel in plugin_panels:
                panel["plugin_name"] = name
                panels.append(panel)
        return panels
    
    def apply_to_app(self, app: FastAPI) -> None:
        """Apply all registered web capabilities to FastAPI app"""
        # Include plugin routers
        for name, router in self.active_routers.items():
            try:
                app.include_router(router, tags=[f"plugin-{name}"])
            except Exception as e:
                logger.warning(f"Failed to mount router for plugin {name}: {e}")
        
        # Mount static assets
        for name, path in self.static_mounts.items():
            try:
                from fastapi.staticfiles import StaticFiles
                app.mount(f"/plugins/{name}/static", 
                         StaticFiles(directory=str(path)), 
                         name=f"plugin-{name}-static")
            except Exception as e:
                logger.warning(f"Failed to mount static files for plugin {name}: {e}")
```

#### B. FastAPI App Integration

```python
# In src/agent_system/agent/interface_api.py (build_app function)
def build_app(config_path: Optional[str] = None) -> FastAPI:
    # ... existing setup ...
    
    # Initialize plugin web registry
    plugin_web_registry = PluginWebRegistry()
    
    # Discover and register plugin web capabilities
    if _mcp_integration and hasattr(_mcp_integration, 'plugin_registry'):
        for name, adapter in _mcp_integration.plugin_registry.plugin_servers.items():
            if hasattr(adapter, 'plugin_server') and isinstance(adapter.plugin_server, PluginWebInterface):
                plugin_web_registry.register_web_plugin(name, adapter.plugin_server)
    
    # Apply plugin web capabilities to app
    plugin_web_registry.apply_to_app(app)
    
    # Add panel discovery endpoint
    @app.get("/api/plugins/panels")
    def get_plugin_panels():
        """Get all available plugin UI panels"""
        return {"panels": plugin_web_registry.get_all_panels()}
    
    # ... rest of existing setup ...
```

### 4. Frontend Panel Framework

#### A. Panel Manager Component

```javascript
// In static/js/panel_manager.js
class PluginPanelManager {
    constructor() {
        this.panels = new Map();
        this.activePanel = null;
        this.container = null;
    }
    
    async initialize() {
        // Fetch available panels from backend
        const response = await fetch('/api/plugins/panels');
        const data = await response.json();
        
        this.createPanelContainer();
        
        for (const panel of data.panels) {
            this.registerPanel(panel);
        }
    }
    
    createPanelContainer() {
        this.container = document.createElement('div');
        this.container.className = 'plugin-panels-container';
        this.container.innerHTML = `
            <div class="panel-tabs"></div>
            <div class="panel-content"></div>
        `;
        document.querySelector('.main-container').appendChild(this.container);
    }
    
    registerPanel(panelConfig) {
        const panel = new PluginPanel(panelConfig);
        this.panels.set(panelConfig.id, panel);
        this.addPanelTab(panel);
    }
    
    addPanelTab(panel) {
        const tabsContainer = this.container.querySelector('.panel-tabs');
        const tab = document.createElement('button');
        tab.className = 'panel-tab';
        tab.textContent = panel.config.title;
        tab.onclick = () => this.activatePanel(panel.config.id);
        tabsContainer.appendChild(tab);
    }
    
    activatePanel(panelId) {
        if (this.activePanel) {
            this.activePanel.hide();
        }
        
        const panel = this.panels.get(panelId);
        if (panel) {
            panel.show();
            this.activePanel = panel;
        }
    }
}

class PluginPanel {
    constructor(config) {
        this.config = config;
        this.iframe = null;
        this.container = null;
    }
    
    show() {
        if (!this.iframe) {
            this.createIframe();
        }
        
        this.container.style.display = 'block';
        this.setupMessageHandling();
    }
    
    hide() {
        if (this.container) {
            this.container.style.display = 'none';
        }
    }
    
    createIframe() {
        this.container = document.createElement('div');
        this.container.className = 'plugin-panel-frame';
        this.container.style.display = 'none';
        
        this.iframe = document.createElement('iframe');
        this.iframe.src = this.config.url;
        this.iframe.style.width = this.config.width || '100%';
        this.iframe.style.height = this.config.height || '400px';
        this.iframe.style.border = 'none';
        
        this.container.appendChild(this.iframe);
        document.querySelector('.plugin-panels-container .panel-content').appendChild(this.container);
    }
    
    setupMessageHandling() {
        // Listen for messages from plugin iframe
        window.addEventListener('message', (event) => {
            if (event.source !== this.iframe.contentWindow) return;
            
            this.handlePluginMessage(event.data);
        });
    }
    
    handlePluginMessage(data) {
        switch (data.type) {
            case 'api_request':
                this.handleApiRequest(data.payload);
                break;
            case 'resize':
                this.resizePanel(data.width, data.height);
                break;
            case 'notification':
                this.showNotification(data.message, data.level);
                break;
        }
    }
    
    async handleApiRequest(payload) {
        try {
            const response = await fetch(payload.url, payload.options);
            const result = await response.json();
            
            // Send response back to plugin iframe
            this.iframe.contentWindow.postMessage({
                type: 'api_response',
                requestId: payload.requestId,
                data: result
            }, '*');
        } catch (error) {
            this.iframe.contentWindow.postMessage({
                type: 'api_error', 
                requestId: payload.requestId,
                error: error.message
            }, '*');
        }
    }
}
```

#### B. Plugin Client SDK

```javascript
// Plugin SDK for iframe communication (served as static asset)
// /plugins/{name}/static/plugin-sdk.js
class PluginSDK {
    constructor() {
        this.requestId = 0;
        this.pendingRequests = new Map();
        this.setupMessageListener();
    }
    
    setupMessageListener() {
        window.addEventListener('message', (event) => {
            if (event.data.type === 'api_response') {
                this.handleApiResponse(event.data);
            } else if (event.data.type === 'api_error') {
                this.handleApiError(event.data);
            }
        });
    }
    
    async apiRequest(url, options = {}) {
        const requestId = ++this.requestId;
        
        return new Promise((resolve, reject) => {
            this.pendingRequests.set(requestId, { resolve, reject });
            
            parent.postMessage({
                type: 'api_request',
                payload: {
                    requestId,
                    url,
                    options
                }
            }, '*');
        });
    }
    
    handleApiResponse(data) {
        const request = this.pendingRequests.get(data.requestId);
        if (request) {
            request.resolve(data.data);
            this.pendingRequests.delete(data.requestId);
        }
    }
    
    handleApiError(data) {
        const request = this.pendingRequests.get(data.requestId);
        if (request) {
            request.reject(new Error(data.error));
            this.pendingRequests.delete(data.requestId);
        }
    }
    
    resize(width, height) {
        parent.postMessage({
            type: 'resize',
            width,
            height
        }, '*');
    }
    
    notify(message, level = 'info') {
        parent.postMessage({
            type: 'notification',
            message,
            level
        }, '*');
    }
}

// Auto-initialize SDK
window.pluginSDK = new PluginSDK();
```

### 5. Security Model

#### A. Authentication & Authorization

```python
# In src/agent_system/plugins/security.py
class PluginSecurityManager:
    def __init__(self):
        self.plugin_permissions: Dict[str, Set[str]] = {}
        self.auth_handlers: Dict[str, Callable] = {}
    
    def register_plugin_permissions(self, plugin_name: str, permissions: Set[str]):
        """Register permissions for a plugin"""
        self.plugin_permissions[plugin_name] = permissions
    
    def check_permission(self, plugin_name: str, permission: str) -> bool:
        """Check if plugin has specific permission"""
        plugin_perms = self.plugin_permissions.get(plugin_name, set())
        return permission in plugin_perms
    
    async def authenticate_plugin_request(self, request: Request, plugin_name: str) -> bool:
        """Authenticate a request to a plugin endpoint"""
        # Basic auth check - extend as needed
        auth_header = request.headers.get("Authorization", "")
        
        # For now, allow all authenticated requests
        # TODO: Implement proper plugin-specific auth
        return bool(auth_header)

def create_plugin_security_middleware(security_manager: PluginSecurityManager):
    """Create FastAPI middleware for plugin security"""
    
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        
        # Check if this is a plugin endpoint
        if path.startswith("/plugins/"):
            parts = path.split("/")
            if len(parts) >= 3:
                plugin_name = parts[2]
                
                # Check authentication if required
                config = get_plugin_security_config(plugin_name)
                if config.get("require_auth", False):
                    if not await security_manager.authenticate_plugin_request(request, plugin_name):
                        return Response(status_code=401)
        
        return await call_next(request)
    
    return security_middleware
```

#### B. Content Security Policy

```python
# CSP headers for plugin iframes
def apply_plugin_csp_headers(response: Response, plugin_name: str):
    """Apply Content Security Policy headers for plugin responses"""
    
    # Basic CSP for plugin iframes
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
```

### 6. Communication Protocols

#### A. Plugin-to-Host API

```javascript
// Plugin iframe can make API calls through postMessage
const data = await pluginSDK.apiRequest('/api/status', {
    method: 'GET',
    headers: {'Accept': 'application/json'}
});

// Plugin can request host services
const agents = await pluginSDK.apiRequest('/api/agents/stats');
const status = await pluginSDK.apiRequest('/status/meta');
```

#### B. Host-to-Plugin Events

```javascript
// Host can send events to plugin iframes
class PluginEventBroadcaster {
    broadcast(eventType, data) {
        this.panels.forEach(panel => {
            if (panel.iframe && panel.iframe.contentWindow) {
                panel.iframe.contentWindow.postMessage({
                    type: 'host_event',
                    eventType,
                    data
                }, '*');
            }
        });
    }
}

// Usage: broadcast status updates to all plugin panels
eventBroadcaster.broadcast('status_update', statusEvent);
```

### 7. Plugin Development Example

```python
# Example: Real-time log viewer plugin
class LogViewerPlugin(PluginWebInterface):
    def __init__(self, name: str, config: Dict[str, Any], ssl_verify: bool = True):
        self.name = name
        self.config = config
        
    def get_web_router(self) -> APIRouter:
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        @router.get("/logs/stream")
        async def stream_logs():
            async def log_generator():
                # Stream log file content
                with open("logs/agent.log", "r") as f:
                    f.seek(0, 2)  # Go to end
                    while True:
                        line = f.readline()
                        if line:
                            yield f"data: {json.dumps({'line': line.strip()})}\n\n"
                        else:
                            await asyncio.sleep(0.1)
            
            return StreamingResponse(log_generator(), media_type="text/event-stream")
        
        @router.get("/panel.html", response_class=HTMLResponse)
        async def panel_html():
            return """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Log Viewer</title>
                <script src="/plugins/log_viewer/static/plugin-sdk.js"></script>
            </head>
            <body>
                <div id="log-container"></div>
                <script>
                    const eventSource = new EventSource('/plugins/log_viewer/logs/stream');
                    eventSource.onmessage = function(event) {
                        const data = JSON.parse(event.data);
                        const container = document.getElementById('log-container');
                        container.innerHTML += data.line + '<br>';
                        container.scrollTop = container.scrollHeight;
                    };
                </script>
            </body>
            </html>
            """
        
        return router
    
    def get_panels(self) -> List[Dict[str, Any]]:
        return [{
            "id": "log_viewer_panel",
            "title": "System Logs",
            "url": f"/plugins/{self.name}/panel.html",
            "icon": "file-text",
            "position": "bottom",
            "width": "100%", 
            "height": "200px"
        }]
    
    def get_security_config(self) -> Dict[str, Any]:
        return {
            "require_auth": True,
            "rate_limit": "10/minute"
        }
```

## Implementation Plan

1. **Phase 1**: Extend plugin base classes and registry
2. **Phase 2**: Integrate web registry with FastAPI app  
3. **Phase 3**: Implement frontend panel manager
4. **Phase 4**: Create plugin SDK and communication layer
5. **Phase 5**: Add security middleware and policies
6. **Phase 6**: Build example log viewer plugin
7. **Phase 7**: Documentation and testing

## Benefits

- **Rich UIs**: Plugins can provide full HTML/JS/CSS interfaces
- **Real-time data**: SSE/WebSocket support for live updates  
- **Secure**: Proper sandboxing and authentication
- **Flexible**: Panels can be positioned and resized
- **Integrated**: Seamless communication with host application
- **Backwards Compatible**: Existing MCP-only plugins continue working

This design leverages the existing plugin architecture while adding powerful web capabilities, enabling plugins to create sophisticated user interfaces beyond simple tool calls.