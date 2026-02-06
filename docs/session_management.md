# Session Management System

## Overview

The AgentSystem now supports persistent conversation sessions with multi-user support. Sessions are stored as JSON files on disk, allowing conversations to survive server restarts and be accessed across devices.

## Architecture

### Storage Structure

```
data/sessions/
  ├── {user_id}/
  │   ├── {session_id}.json
  │   ├── {session_id}.json
  │   └── .backup_{session_id}_{timestamp}.json  (deleted sessions)
  └── anonymous/
      └── {session_id}.json
```

### Session Schema

Each session is a JSON file containing:

```json
{
  "session_id": "abc123def456",
  "user_id": "username",
  "created_at": "2025-10-12T10:30:00Z",
  "updated_at": "2025-10-12T12:45:00Z",
  "title": "Conversation Title",
  "agent_name": "basic_agent",
  "llm_profile": "default",
  "context_vars": {
    "workflow_phase": "planning",
    "book_id": "123"
  },
  "messages": [
    {
      "role": "user",
      "content": "Hello",
      "estimated_tokens": 3
    },
    {
      "role": "assistant",
      "content": "Hi there! How can I help you today?",
      "estimated_tokens": 12,
      "tool_calls": null
    }
  ],
  "metadata": {
    "message_count": 2,
    "token_count": 150,
    "last_agent_response": "Hi there!",
    "tags": ["important", "work"],
    "custom_field": "custom_value"
  }
}
```

#### Message Fields

Each message in the `messages` array includes:

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | Message role: `user`, `assistant`, `tool`, or `system` |
| `content` | string/array | Message content (text or multimodal) |
| `estimated_tokens` | int | Estimated token count for context window tracking |
| `tool_calls` | array | Tool calls made by assistant (optional) |
| `tool_call_id` | string | ID reference for tool responses (optional) |
| `timestamp` | string | ISO 8601 timestamp (optional) |
| `reasoning_content` | string | Chain-of-thought reasoning (optional) |

The `estimated_tokens` field is computed when the session is saved using `~4 chars/token` for text and `~1000 tokens` for images.
```

## API Endpoints

All session endpoints require authentication (JWT token or API key).

### Create Session

```http
POST /api/sessions
Content-Type: application/json
Authorization: Bearer <token>

{
  "title": "New Conversation",
  "agent_name": "basic_agent",
  "llm_profile": "default",
  "session_id": "optional-custom-id"
}

Response:
{
  "session_id": "abc123def456",
  "status": "created"
}
```

### List Sessions

```http
GET /api/sessions
Authorization: Bearer <token>

Response:
[
  {
    "session_id": "abc123def456",
    "user_id": "username",
    "title": "Conversation Title",
    "agent_name": "basic_agent",
    "llm_profile": "default",
    "created_at": "2025-10-12T10:30:00Z",
    "updated_at": "2025-10-12T12:45:00Z",
    "message_count": 15,
    "last_agent_response": "...",
    "tags": ["work"]
  }
]
```

### Get Session

Retrieve a session with full conversation history:

```http
GET /api/sessions/{session_id}
Authorization: Bearer <token>

Response: <full session JSON>
```

### Update Session

Update session metadata (title, tags, custom fields):

```http
PATCH /api/sessions/{session_id}
Content-Type: application/json
Authorization: Bearer <token>

{
  "title": "Updated Title",
  "tags": ["important", "work"],
  "metadata": {
    "priority": "high"
  }
}

Response:
{
  "status": "updated",
  "session_id": "abc123def456"
}
```

### Delete Session

Delete a session (creates backup by default):

```http
DELETE /api/sessions/{session_id}?create_backup=true
Authorization: Bearer <token>

Response:
{
  "status": "deleted",
  "session_id": "abc123def456"
}
```

### Restore Session

Load a session for continuation:

```http
POST /api/sessions/{session_id}/restore
Authorization: Bearer <token>

Response:
{
  "status": "ready",
  "session_id": "abc123def456",
  "message_count": 15,
  "title": "Conversation Title"
}
```

## Python API

### SessionManager

```python
from agent_system.services import SessionManager

# Initialize
manager = SessionManager(storage_path="data/sessions")

# Create session
session = await manager.create_session(
    user_id="username",
    title="My Conversation",
    agent_name="basic_agent",
    llm_profile="gpt-4"
)

# Load session
session = await manager.load_session("username", session_id)

# Save changes
session["messages"].append({"role": "user", "content": "Hello"})
await manager.save_session(session)

# List sessions
sessions = await manager.list_sessions("username")

# Delete session
await manager.delete_session("username", session_id, create_backup=True)

# Rename
await manager.rename_session("username", session_id, "New Title")

# Update metadata
await manager.update_session_metadata(
    "username",
    session_id,
    tags=["important"],
    custom_field="value"
)
```

## UI Integration

### Session Sidebar

The UI includes a session sidebar for managing conversations:

- **Toggle Button**: Left side of screen opens/closes sidebar
- **New Conversation**: Creates a fresh session
- **Session List**: Shows all user sessions, sorted by recent activity
- **Session Actions**: Rename or delete individual sessions
- **Active Indicator**: Highlights current session

### Keyboard Shortcuts

- Click session to load/restore conversation
- Rename modal supports Enter to save, Esc to cancel

### Page Refresh

Sessions are automatically restored after page refresh via localStorage.

## Features

### Multi-User Support

- Sessions are isolated by user_id
- Users cannot access other users' sessions
- Anonymous sessions supported for non-authenticated users

### Caching

- LRU cache with 5-minute TTL
- Reduces disk I/O for frequently accessed sessions
- Cache cleared on manual refresh

### Atomic Writes

- All writes use temp file + atomic replace pattern
- Prevents corruption from interrupted writes
- Thread-safe with asyncio.Lock

### Security

- Directory traversal protection (sanitized user_ids)
- Permission checks on all operations
- Backup creation on delete (optional)

### Performance

- Lazy loading (metadata only for list view)
- Efficient JSON serialization
- No global index (directory structure serves as index)

## Migration

For migrating old session data:

```bash
# Dry run (show what would be migrated)
python -m scripts.migrate_sessions --dry-run

# Actual migration
python -m scripts.migrate_sessions

# Create sample sessions for testing
python -m scripts.migrate_sessions --create-samples 5
```

## Testing

```bash
# Unit tests (SessionManager CRUD)
pytest tests/test_session_manager_crud.py -v

# Integration tests (end-to-end scenarios)
pytest tests/test_session_integration.py -v

# All session tests
pytest tests/test_session*.py -v
```

## Configuration

No additional configuration required. Storage path defaults to `data/sessions/` relative to project root.

Optional: Customize via SessionManager constructor:

```python
manager = SessionManager(storage_path="/custom/path")
```

## Error Handling

Custom exceptions:

- `SessionNotFoundError`: Session doesn't exist
- `SessionPermissionError`: User doesn't own session
- `ValueError`: Invalid input (duplicate ID, missing fields, etc.)

All exceptions include descriptive messages for debugging.

## Best Practices

1. **Always use user_id from authenticated user** - Never trust client-provided user_id
2. **Call save_session after modifying messages** - Changes aren't persisted automatically
3. **Handle exceptions appropriately** - Don't expose internal errors to end users
4. **Use session titles wisely** - First 100 chars of task used as default
5. **Clean up old sessions periodically** - Implement retention policy if needed

## Limitations

- No real-time sync between multiple browser tabs (localStorage-based)
- No server-side session expiration (retention policy must be implemented separately)
- No compression (large conversations stored as-is)
- No encryption at rest (filesystem-level encryption recommended for sensitive data)

## Future Enhancements

- Real-time session sync via WebSockets
- Session sharing/collaboration
- Export/import conversations
- Session analytics and insights
- Compression for large sessions
- Cloud storage backend option
