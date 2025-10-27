# Sequential Thinking Panel - Implementation Guide

**Quick Reference**: This document provides a concise implementation plan for the Sequential Thinking visualization panel using existing WebUI infrastructure.

**Full Design**: See [`sequential_thinking_panel_design.md`](./sequential_thinking_panel_design.md) (comprehensive 14-section design) and **Appendix D** (revised implementation).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│        Sequential Thinking Panel (PanelManager)         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Sessions     │  │ Graph (D3)   │  │ Details      │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │ SSE + REST
┌──────────────────────▼──────────────────────────────────┐
│  ThinkingWebFactory (web_endpoints.py)                  │
│  - /panel (HTML iframe)                                 │
│  - /sessions (JSON)                                     │
│  - /sessions/{id}/stream (SSE)                          │
└─────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Location |
|-----------|-----------|----------|
| Panel System | `PanelManager.createPanel()` | `static/js/panel_manager.js` |
| Module | `window.AgentSystem.ThinkingPanel` | `static/js/thinking_panel_module.js` |
| State | Plain objects + Map | In-memory (no Zustand) |
| Real-time | EventSource (SSE) | Browser native API |
| Graph Viz | D3.js v7 (tree layout) | CDN |
| Backend | FastAPI Router | `web_endpoints.py` |
| Template | Jinja2 | `templates/panel.html` |

---

## Implementation Checklist

### Phase 1: Backend (Server-Side)

- [ ] **Create `src/plugins/sequential_thinking/web_endpoints.py`**
  ```python
  class ThinkingWebFactory:
      def __init__(self, server):
          self.server = server  # SequentialThinkingServer
          self.templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
      
      def get_web_router(self) -> APIRouter:
          router = APIRouter(prefix=f"/plugins/{self.server.name}")
          
          @router.get("/panel", response_class=HTMLResponse)
          async def get_panel(request: Request):
              return self.templates.TemplateResponse("panel.html", {"request": request})
          
          @router.get("/sessions")
          async def get_sessions():
              return {"sessions": list(self.server.sessions.values())}
          
          @router.get("/sessions/{session_id}/stream")
          async def stream_session(session_id: str):
              # SSE implementation
              pass
          
          return router
  ```

- [ ] **Update `src/plugins/sequential_thinking/server.py`**
  - Add event queue: `self.event_queues: Dict[str, asyncio.Queue] = {}`
  - Emit events in `add_thought()`:
    ```python
    await self._emit_event(session_id, {
        "event": "thought_created",
        "data": {"thought_number": thought.server_thought_number, ...}
    })
    ```
  - Emit events in `switch_branch()`, `create_branch()`
  - Add `_emit_event()` helper to broadcast to all queues

- [ ] **Update `src/plugins/sequential_thinking/schema.yaml`**
  ```yaml
  web_ui:
    button:
      enabled: true
      text: "🧠 Thinking"
      icon: "🧠"
      tooltip: "View sequential thinking visualization"
    
    panel:
      enabled: true
      title: "Sequential Thinking"
      endpoint: "/plugins/{{ name }}/panel"
      type: "iframe"
      width: "1200px"
      height: "800px"
  ```

- [ ] **Create `src/plugins/sequential_thinking/templates/panel.html`**
  ```html
  <!DOCTYPE html>
  <html>
  <head>
      <title>Sequential Thinking</title>
      <script src="https://d3js.org/d3.v7.min.js"></script>
      <link rel="stylesheet" href="/static/css/thinking_panel.css">
  </head>
  <body>
      <div id="thinking-panel-root">
          <div id="thinking-sessions-list"></div>
          <div id="thinking-graph-container"></div>
          <div id="thinking-details-panel"></div>
      </div>
      <script src="/static/js/thinking_panel_module.js"></script>
      <script>
          // Initialize panel
          AgentSystem.ThinkingPanel.init();
      </script>
  </body>
  </html>
  ```

- [ ] **Register router in plugin initialization**
  - In `server.py` `__init__` or separate registration:
    ```python
    from .web_endpoints import ThinkingWebFactory
    
    def register_web_routes(app: FastAPI, server):
        factory = ThinkingWebFactory(server)
        app.include_router(factory.get_web_router())
    ```

### Phase 2: Frontend (Client-Side)

- [ ] **Create `static/js/thinking_panel_module.js`**
  ```javascript
  window.AgentSystem = window.AgentSystem || {};
  
  window.AgentSystem.ThinkingPanel = (function() {
      'use strict';
      
      const state = {
          sessions: new Map(),
          currentSessionId: null,
          eventSource: null
      };
      
      function showPanel() {
          const content = `<div id="thinking-panel-container"></div>`;
          window.AgentSystem.PanelManager.createPanel(
              'thinking-panel',
              '🧠 Sequential Thinking',
              content,
              ['thinking-panel']
          );
          loadSessions();
          setupEventStream();
      }
      
      async function loadSessions() {
          const response = await fetch('/plugins/sequential_thinking/sessions');
          const data = await response.json();
          state.sessions = new Map(data.sessions.map(s => [s.id, s]));
          renderSessionList();
      }
      
      function setupEventStream() {
          if (state.eventSource) state.eventSource.close();
          const sessionId = state.currentSessionId;
          if (!sessionId) return;
          
          state.eventSource = new EventSource(`/plugins/sequential_thinking/sessions/${sessionId}/stream`);
          state.eventSource.addEventListener('thought_created', (e) => {
              const data = JSON.parse(e.data);
              handleThoughtCreated(data);
          });
      }
      
      function renderSessionList() { /* DOM manipulation */ }
      function renderThoughtGraph() { /* D3.js tree layout */ }
      
      return { showPanel, selectSession, close };
  })();
  ```

- [ ] **Create `static/css/thinking_panel.css`**
  ```css
  #thinking-panel-root {
      display: grid;
      grid-template-columns: 250px 1fr 350px;
      height: 100%;
      gap: 10px;
  }
  
  #thinking-sessions-list { /* Session list styles */ }
  #thinking-graph-container { /* Graph canvas styles */ }
  #thinking-details-panel { /* Details panel styles */ }
  
  .thought-node { /* D3 node styles */ }
  .thought-link { /* D3 link styles */ }
  .thought-revision { fill: orange; }
  .thought-normal { fill: steelblue; }
  ```

- [ ] **Implement D3.js graph visualization**
  - Build hierarchy from thoughts (parent-child relationships)
  - Use `d3.tree()` for layout
  - Render nodes (circles) and links (paths)
  - Add interactivity (click, hover, zoom)
  - Color-code by branch/revision status

### Phase 3: Integration & Testing

- [ ] **Test plugin UI registration**
  - Start API server
  - Navigate to WebUI
  - Verify "🧠 Thinking" button appears
  - Click button, verify panel opens

- [ ] **Test REST endpoints**
  - `/plugins/sequential_thinking/sessions` returns session data
  - `/plugins/sequential_thinking/sessions/{id}` returns session details

- [ ] **Test SSE stream**
  - Open panel
  - Create thought via MCP tool call
  - Verify SSE event fires
  - Verify graph updates in real-time

- [ ] **Test graph rendering**
  - Create 10+ thoughts
  - Create 2+ branches
  - Create revision
  - Verify graph renders correctly
  - Verify zoom/pan works

- [ ] **E2E workflow**
  - Start new thinking session
  - Add thoughts sequentially
  - Create branch
  - Switch between branches
  - Create revision
  - Verify panel shows all operations

### Phase 4: Polish & Documentation

- [ ] **UI/UX refinements**
  - Responsive layout
  - Loading indicators
  - Error states
  - Empty states
  - Keyboard shortcuts

- [ ] **Performance optimization**
  - Virtual scrolling for 100+ thoughts
  - Debounce graph re-renders
  - Connection pooling for SSE

- [ ] **Documentation**
  - User guide: how to use panel
  - Developer guide: how to extend
  - Architecture diagram
  - API reference

---

## API Endpoints Reference

| Endpoint | Method | Response | Description |
|----------|--------|----------|-------------|
| `/plugins/sequential_thinking/panel` | GET | HTML | Panel iframe (Jinja2 template) |
| `/plugins/sequential_thinking/sessions` | GET | JSON | `{sessions: []}` |
| `/plugins/sequential_thinking/sessions/{id}` | GET | JSON | `{id, thoughts, branches, ...}` |
| `/plugins/sequential_thinking/sessions/{id}/stream` | GET | SSE | `event: thought_created\ndata: {...}` |
| `/plugins/sequential_thinking/sessions/{id}/thoughts` | GET | JSON | `{thoughts: []}` |
| `/plugins/sequential_thinking/sessions/{id}/branches` | GET | JSON | `{branches: []}` |

---

## Event Schema

### Thought Created
```json
{
  "event": "thought_created",
  "session_id": "abc123",
  "data": {
    "thought_number": 5,
    "server_thought_number": 5,
    "thought": "Analyzing data...",
    "is_revision": false,
    "branch_id": "main",
    "timestamp": "2025-01-15T10:30:00Z"
  }
}
```

### Branch Switched
```json
{
  "event": "branch_switched",
  "session_id": "abc123",
  "data": {
    "from_branch": "main",
    "to_branch": "alternative",
    "branch_point": 8,
    "timestamp": "2025-01-15T10:35:00Z"
  }
}
```

---

## Dependencies

**New Dependencies**:
- D3.js v7 (CDN): `https://d3js.org/d3.v7.min.js`

**Existing Dependencies** (no changes):
- FastAPI (backend)
- Jinja2 (templates)
- PanelManager (frontend)
- EventSource (browser native)

---

## Testing Checklist

- [ ] Unit tests: `web_endpoints.py` (pytest)
- [ ] Integration tests: SSE events (pytest)
- [ ] E2E tests: Panel workflow (manual or Playwright)
- [ ] Performance tests: 100+ thoughts (manual)
- [ ] Browser compatibility: Chrome, Firefox, Safari (manual)

---

## Reference Files

**Existing Patterns to Follow**:
- `src/plugins/todo/web_endpoints.py` - Web factory pattern
- `src/plugins/todo/schema.yaml` - UI registration
- `static/js/status_module.js` - Panel module pattern
- `static/js/chat_module.js` - EventSource usage (line 531)

**Files to Create**:
- `src/plugins/sequential_thinking/web_endpoints.py` (NEW)
- `src/plugins/sequential_thinking/templates/panel.html` (NEW)
- `static/js/thinking_panel_module.js` (NEW)
- `static/css/thinking_panel.css` (NEW)

**Files to Modify**:
- `src/plugins/sequential_thinking/schema.yaml` (add `web_ui` section)
- `src/plugins/sequential_thinking/server.py` (add event emission)

---

## Estimated Effort

- **Phase 1** (Backend): 1-2 days
- **Phase 2** (Frontend): 2-3 days
- **Phase 3** (Integration): 1 day
- **Phase 4** (Polish): 1-2 days

**Total**: 3-5 days (significantly faster than React rewrite)

---

## Success Criteria

- ✅ Panel opens when button clicked
- ✅ Session list displays correctly
- ✅ Graph renders thought structure
- ✅ Real-time updates on thought creation
- ✅ Handles 100+ thoughts smoothly
- ✅ No console errors
- ✅ Works in Chrome, Firefox, Safari
