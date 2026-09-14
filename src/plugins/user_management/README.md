# User Management Plugin

Web-based user administration interface for AgentSystem's multi-user authentication system.

## Overview

This plugin provides a comprehensive web UI for managing users when multi-user authentication is enabled. It integrates with the auth system to provide admin-level user management capabilities.

## Features

- **User Dashboard**: View all users with search and filtering
- **User Statistics**: Real-time count of total, active, and role-based users
- **User Actions**:
  - Toggle user active/inactive status
  - Delete users with confirmation
  - View user details (ID, username, email, role, creation date, last login)
  - API key indicators
- **Role Management**: Visual badges for ADMIN, USER, and GUEST roles
- **Search Functionality**: Client-side search across usernames, emails, and names
- **Responsive Design**: Modern, gradient-based UI

## Requirements

- AgentSystem with `auth.enabled: true` in configuration
- Admin privileges to access the plugin

## Installation

The plugin is included in the AgentSystem plugins directory. No additional installation required.

## Configuration

In `config/mcp.yaml` or your plugin configuration:

```yaml
servers:
  user_management:
    type: plugin
    plugin_name: user_management
    enabled: true
    mcp:
      items_per_page: 20          # Number of users per page
      allow_self_delete: false     # Allow users to delete themselves
      show_api_keys: true          # Show API key indicators
      require_email_validation: false  # Require email validation (future)
```

## Usage

### Access the Dashboard

Navigate to: `http://127.0.0.1:8000/plugins/user_management/`

**Note**: This endpoint requires admin authentication when auth is enabled.

### API Endpoints

All endpoints are prefixed with `/plugins/user_management/`:

- `GET /` - User management dashboard (HTML)
- `GET /users/list?skip=0&limit=20` - List users (JSON)
- `GET /users/{user_id}` - Get user details (JSON)
- `POST /users/{user_id}/toggle-active` - Toggle user active status
- `POST /users/{user_id}/change-role?role=ADMIN` - Change user role
- `DELETE /users/{user_id}` - Delete user
- `GET /stats` - Get user statistics (JSON)

### Dashboard Features

**Search Bar**: Type in the search box to filter users by username, email, or full name

**User Actions**:
- **⏸/▶ Toggle**: Activate or deactivate user accounts
- **✏️ Edit**: View user details (integration point for future edit functionality)
- **🗑️ Delete**: Remove users with confirmation dialog

**Statistics**:
- Total user count displayed in header
- Active user count auto-updates based on current filter
- Role distribution visible via colored badges

## Security

- All endpoints require authentication when `auth.enabled: true`
- Admin role required for user management operations
- Self-delete protection (configurable via `allow_self_delete`)
- Confirmation dialogs for destructive operations
- Rate limiting applies to all endpoints (inherited from auth middleware)

## Integration Points

### With Auth System

The plugin integrates with `src/agent_system/auth/database.py`:
- Uses `get_db()` to access UserDatabase
- Leverages existing CRUD operations
- Respects user roles and permissions

### With Admin Endpoints

Complements the `/admin/users/*` REST API endpoints with a visual interface.

### Panel Registration

The plugin registers admin panels accessible from the main UI:
- **User Management**: Main dashboard at `/plugins/user_management/`
- **User Statistics**: Stats endpoint at `/plugins/user_management/stats`

## Development

### File Structure

```
user_management/
├── __init__.py           # Package initialization
├── plugin.py             # Plugin factory and main class
├── plugin.yaml           # Plugin metadata
├── endpoints.py          # FastAPI web endpoints
├── README.md             # This file
├── templates/
│   ├── dashboard.html    # Main user management UI
│   └── auth_disabled.html # Auth not enabled message
└── static/               # Future: JS/CSS assets
```

### Adding New Features

1. **Add API Endpoint**: Update `endpoints.py` with new route
2. **Update UI**: Modify `dashboard.html` template
3. **Add JavaScript**: Create handler in `<script>` section
4. **Update Config**: Add new settings to `plugin.yaml`

### Testing

```bash
# Enable auth in config
auth:
  enabled: true
  secret_key: "test-key-min-32-chars-long-secure"

# Start API
agent-api

# Create admin user (USERNAME EMAIL; prompts for the password)
agent-cli users create admin admin@test.com --admin

# Login and access dashboard
curl -X POST http://127.0.0.1:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your_password"}'

# Visit http://127.0.0.1:8000/plugins/user_management/
```

## Future Enhancements

- [ ] Inline user editing with modal dialogs
- [ ] Bulk user operations (multi-select)
- [ ] User creation form in UI
- [ ] Password reset functionality
- [ ] User activity logs
- [ ] Export users to CSV
- [ ] Pagination controls for large user lists
- [ ] Advanced filtering (by role, status, creation date)
- [ ] User profile page with detailed information
- [ ] API key management UI

## Troubleshooting

### "Authentication Not Enabled" Page

**Solution**: Enable auth in `config/config.yaml`:

```yaml
auth:
  enabled: true
```

### 403 Forbidden Errors

**Cause**: User lacks admin privileges

**Solution**: Promote user to admin:

```bash
agent-cli users update USERNAME --role admin
```

### Users Not Loading

**Check**:
1. Database file exists: `data/users.db`
2. Database permissions are correct
3. Auth system initialized properly
4. Check API logs for errors

### Empty User List

**Cause**: No users created yet

**Solution**: Create users via CLI or API:

```bash
agent-cli users create newuser newuser@example.com
```

## Related Documentation

- [Multi-User Authentication Guide](../../../docs/multi_user_authentication.md)
- [Plugin Authoring Guide](../../../docs/plugin_authoring.md)
- [Auth System Configuration](../../../config/config.yaml)

## Support

For issues or questions:
- Check the main documentation in `docs/`
- Review test cases in `tests/test_auth_system.py`
- Examine auth endpoints in `src/api/auth_endpoints.py` and `src/api/admin_endpoints.py`

## License

Part of the AgentSystem project. See main LICENSE file.
