# Multi-User Authentication and Authorization

## Overview

The AgentSystem now supports multi-user authentication and authorization, enabling multiple users to access the API with isolated sessions and proper access controls. This feature is **disabled by default** and can be enabled through configuration.

## Security Architecture

When deploying to the public internet, the AgentSystem implements a multi-layered security approach:

```
┌─────────────────────────────────────────────────────────────────┐
│                    LAYER 1: Transport Security                   │
│                 (HTTPS, TLS - handled externally)                │
├─────────────────────────────────────────────────────────────────┤
│                    LAYER 2: Rate Limiting                        │
│           Per-IP request limiting (RateLimitMiddleware)          │
├─────────────────────────────────────────────────────────────────┤
│                    LAYER 3: Authentication                       │
│              JWT Token / API Key / Anonymous Mode                │
├─────────────────────────────────────────────────────────────────┤
│                    LAYER 4: Authorization                        │
│           Role-based (ADMIN/USER/GUEST) + Resource ACL           │
├─────────────────────────────────────────────────────────────────┤
│                    LAYER 5: LLM Request Security                 │
│        User context validation for all LLM API calls             │
└─────────────────────────────────────────────────────────────────┘
```

### Security Enforcement (v0.5.1+)

The `EndpointSecurityEnforcer` class (`src/agent_system/auth/enforcement.py`) provides centralized security enforcement:

- **Endpoint Pattern Matching**: Rules like `/admin/*` or `POST /run` can require specific roles
- **Anonymous Access Control**: Configurable list of endpoints accessible without authentication
- **LLM Request Validation**: Ensures LLM API calls are only made by authenticated users
- **Session Ownership**: Users can only access their own sessions

## Components

1. **User Database (`src/agent_system/auth/database.py`)**
   - SQLite-based user storage (with PostgreSQL migration path)
   - User CRUD operations
   - API key generation and management
   - Last login tracking

2. **Security Module (`src/agent_system/auth/security.py`)**
   - Password hashing using bcrypt (direct implementation)
   - JWT token generation and validation
   - API key generation, hashing, and verification
   - Configurable token expiration

3. **Authentication Middleware (`src/agent_system/auth/middleware.py`)**
   - Rate limiting (60 requests/minute per IP)
   - Security headers (X-Frame-Options, CSP, HSTS, etc.)
   - CORS configuration

4. **Security Enforcement (`src/agent_system/auth/enforcement.py`)** *(NEW)*
   - Centralized endpoint security enforcement
   - Role-based access control validation
   - Anonymous user handling
   - LLM request authorization

5. **User Models (`src/agent_system/auth/models.py`)**
   - Pydantic models for validation
   - User roles: ADMIN, USER, GUEST
   - Token and API key schemas

6. **FastAPI Dependencies (`src/agent_system/auth/dependencies.py`)**
   - `get_current_user`: Extract user from JWT or API key
   - `get_current_active_user`: Ensure user is active
   - `require_admin`: Restrict access to admin users
   - `get_optional_user`: Allow both authenticated and anonymous access

7. **API Endpoints**
   - **Auth Endpoints** (`src/agent_system/api/auth_endpoints.py`): `/auth/register`, `/auth/login`, `/auth/logout`, `/auth/me`, API key management
   - **Admin Endpoints** (`src/agent_system/api/admin_endpoints.py`): `/admin/users/*` for user management (admin-only)

8. **CLI Commands (`src/agent_system/cli_utils/users.py`)**
   - `agent-cli users list`: List all users
   - `agent-cli users create`: Create a new user
   - `agent-cli users delete`: Delete a user
   - `agent-cli users update`: Update user details
   - `agent-cli users info`: Show user information
   - `agent-cli users generate-api-key`: Generate API key
   - `agent-cli users revoke-api-key`: Revoke API key

### User Roles

- **ADMIN**: Full access including user management
- **USER**: Standard access to API features
- **GUEST**: Limited read-only access

### Which agents a role may run

An agent's `metadata.min_role` (`guest`, `user` or `admin`) is the lowest role
that may run it -- on every path a run starts (`/run`, `/events`,
`/chat/command`, sessions created for it, sub-agents, agents called as tools,
stategraph, woken sessions, the OpenAI-compatible API) and for every tool the
agent serves. Without it an agent runs for every account. Details:
`docs/agent_visibility.md`; which shipped agents carry a gate: SECURITY.md.

### Authentication Methods

1. **JWT Tokens**
   - Bearer token authentication
   - 30-minute expiration (configurable)
   - Header: `Authorization: Bearer <token>`

2. **API Keys**
   - Long-lived authentication
   - Header: `X-API-Key: <key>`, or `Authorization: Bearer <key>` as OpenAI
     clients send it (a Bearer value without dots is a key, never a JWT;
     two different keys in both headers are refused)
   - Plugin routes take a key only when the plugin declares `accept_api_keys`
     in its security config (`openai_api` does); all others stay tokens-only
   - A plugin type that declares `min_role` in its security config
     (`log_viewer`, `ssh_control`: `admin`) keeps that role under any instance
     name; a rule in `plugin_security` may ask for more, never for less
   - Routes that check their admin themselves (`user_management`,
     `agent_editor`) use `get_token_user`: a token only, no key in either
     header — `get_current_user` takes a Bearer key even when called with
     `x_api_key=None`
   - Both layers (middleware, dependencies) read the headers alike: the first
     of a repeated header, the `Bearer` scheme in any case, and of two
     `access_token` cookies the last (both parse the Cookie header with
     Starlette's `cookie_parser`)
   - Hashed with SHA-256 before storage

## Configuration

### Enabling Multi-User Mode

Edit `config/config.yaml`:

```yaml
auth:
  enabled: true  # Set to true to enable multi-user authentication
  secret_key: "your-secret-key-here-CHANGE-IN-PRODUCTION-min-32-chars"  # empty or missing: the API refuses to start
  algorithm: "HS256"
  access_token_expire_minutes: 30
  database_path: "data/users.db"
  
  # Security settings
  rate_limit_enabled: true
  requests_per_minute: 60
  security_headers_enabled: true
  
  # CORS settings
  cors_enabled: true
  cors_origins:
    - "http://localhost:3000"
    - "http://127.0.0.1:8000"
  cors_credentials: true
  
  # Default admin user (created on first startup if no users exist)
  default_admin_username: "admin"
  # default_admin_password: unset on purpose -- the install scripts ask for one
  # (python -m agent_system.auth.first_admin); unset, the first start generates one
  # and shows it on the console
  default_admin_email: "admin@example.com"
  
  # Self-registration through POST /auth/register (reachable without login)
  registration:
    enabled: true            # false: the endpoint answers 403 and creates nobody
    require_approval: false  # true: new accounts start inactive until an admin activates them
    default_role: "user"     # "guest" or "user"; never "admin"

  # ============================================================
  # Anonymous Access Configuration (NEW in v0.5.1)
  # ============================================================
  anonymous_access:
    enabled: false  # Set to true to allow unauthenticated access
    role: "guest"   # Role assigned to anonymous users
    allowed_endpoints:  # Endpoints accessible without auth
      - "GET /health"
      - "GET /static/*"
      - "GET /login"
      - "POST /auth/login"
    rate_limit_multiplier: 0.5  # 50% of normal rate limit
```

The route rules are in `config/security.yaml`, which `config/config.yaml` includes. Edit them there: rules
written into `config.yaml` are replaced by that file's.

```yaml
auth:
  # ============================================================
  # Endpoint Security Rules (NEW in v0.5.1)
  # ============================================================
  endpoint_security:
    default_policy: "require_auth"  # Default: require authentication
    rules:
      - pattern: "/admin/*"
        policy: "require_auth"
        min_role: "admin"
      - pattern: "POST /run"
        policy: "require_auth"
        min_role: "user"
      - pattern: "GET /events"
        policy: "require_auth"
        min_role: "user"
  
  # ============================================================
  # LLM Request Security (NEW in v0.5.1)
  # ============================================================
  llm_security:
    require_valid_user: true          # LLM calls require authenticated user
    validate_session_ownership: true  # Users can only access their own sessions
    audit_llm_requests: true          # Log all LLM requests with user info
    max_requests_per_hour_anonymous: 0  # 0 = anonymous users cannot make LLM requests
```

### Security Best Practices

1. **Secret Key**
   - Use a strong, random secret key (minimum 32 characters)
   - Generate with: `openssl rand -hex 32`
   - Never commit secrets to version control

2. **Default Admin Password**
   - Change the default admin password immediately after first login
   - Use strong passwords (minimum 8 characters, mix of letters, numbers, symbols)

3. **HTTPS**
   - Always use HTTPS in production
   - JWT tokens and API keys are sensitive credentials

4. **Rate Limiting**
   - Adjust `requests_per_minute` based on your needs
   - Monitor for abuse patterns

5. **CORS Configuration**
   - Restrict `cors_origins` to trusted domains only
   - Set `cors_credentials: true` only when necessary — and only together with an
     explicit `cors_origins` allowlist. Combined with the `"*"` wildcard it is
     refused (the middleware drops credentials and warns), because Starlette
     reflects the request Origin instead of sending a literal `*`.

## API Reference

### Authentication Endpoints

#### POST /auth/register
Register a new user. The endpoint is reachable without login (`* /auth/*` is
public), so the caller chooses nothing about its own privileges: `role` and
`is_active` are not accepted (422). `auth.registration` decides the rest:
`enabled: false` answers 403 and creates nobody; `require_approval: true`
creates the account inactive -- it cannot log in (403) until an admin activates
it (user management panel, `POST /admin/users/{id}/activate`,
`agent-cli users update NAME --activate`); `default_role` is `user` or `guest`.
The setting is read on every request, so `agent-cli reload`
(`POST /admin/reload-config`) applies it without a restart.

**Request:**
```json
{
  "username": "johndoe",
  "email": "john@example.com",
  "password": "SecurePass123!",
  "full_name": "John Doe"
}
```

**Response:**
```json
{
  "id": 1,
  "username": "johndoe",
  "email": "john@example.com",
  "full_name": "John Doe",
  "is_active": true,
  "role": "user",
  "created_at": "2025-10-10T20:00:00.123456Z"
}
```

#### POST /auth/login
Login and receive JWT token.

**Request:**
```json
{
  "username": "johndoe",
  "password": "SecurePass123!"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

#### POST /auth/logout
Logout (client-side token disposal).

**Response:**
```json
{
  "message": "Logged out successfully"
}
```

#### GET /auth/me
Get current user information.

**Headers:**
```
Authorization: Bearer <token>
```

**Response:**
```json
{
  "id": 1,
  "username": "johndoe",
  "email": "john@example.com",
  "full_name": "John Doe",
  "is_active": true,
  "role": "user",
  "created_at": "2025-10-10T20:00:00.123456Z"
}
```

#### PATCH /auth/me
Update current user's profile information. Users can update their own email, full name, and password.

**Headers:**
```
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:** (all fields optional)
```json
{
  "email": "newemail@example.com",
  "full_name": "New Full Name",
  "password": "newpassword123",
  "current_password": "oldpassword123"
}
```

A new password requires `current_password` (missing: 400, wrong: 403) and
**ends every earlier login of the account**: a token carries the password
generation under which it was issued (`gen`, table `token_generations`), and a
token from before the change is rejected by the check on every request
(`get_current_user`, security middleware) and by `/auth/refresh` — this includes
a token with no `gen` at all (from before this rule) once the password has been
changed at least once. The browser that makes the change gets a new cookie and
stays logged in; anyone working with a bearer token has to log in again. If
someone else has set the password in the meantime (an admin, also from another
process), that password wins: the change is rejected with 409 and nothing is
saved.

When an admin sets a password (admin API, Users panel, `agent-cli users`), the
logins of that account end in the same way. If an admin sets their **own** in the
admin API or Users panel, their browser gets a new cookie — it is written, as with
`PATCH /auth/me`, only while their login is still valid (if a reset came in
between: 409, nothing saved); via `agent-cli`, their own browser logins end as
well. **The account's API key is also revoked** — it is a login like any other,
and a key created with the old password could be created by anyone who knew it.
Anyone who needs one (`agent-cli reload` reads `AGENT_ADMIN_API_KEY`) creates a
new one afterwards (`POST /auth/api-key`, `agent-cli users generate-api-key`).
Changing the name or e-mail leaves logins and key untouched.

**Response:**
```json
{
  "id": 1,
  "username": "johndoe",
  "email": "newemail@example.com",
  "full_name": "New Full Name",
  "is_active": true,
  "role": "user",
  "created_at": "2025-10-10T20:00:00.123456Z",
  "updated_at": "2025-10-11T10:30:00.123456Z"
}
```

**Example:**
```bash
curl -X PATCH http://localhost:8000/auth/me \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email": "newemail@example.com", "password": "newsecurepassword", "current_password": "oldpassword123"}'
```

#### GET/PUT /auth/me/preferences
Display settings of the logged-in account (today: the chat). `GET` always answers
with all keys, anything not chosen with its default; `PUT` replaces the whole
object, anything omitted becomes the default. Unknown keys and values are
rejected with 422 instead of being silently stored. Stored in the table
`user_preferences` of `users.db` (created by startup itself), deleted with the
account. Without a login: 401; without authentication the endpoint does not exist.

```json
{"chat": {"fold_steps": "at_end", "thinking": "collapsed", "sub_agents": "expanded",
          "sub_agent_output": "collapsed"}}
```

- `fold_steps`: `at_end` (steps collapse when the answer arrives), `at_next_step`
  (as soon as the next one begins), `never`
- `thinking`: `collapsed` | `expanded`
- `sub_agents`: `expanded` | `collapsed` (sub-agent runs in the chat)
- `sub_agent_output`: `collapsed` | `expanded` (a sub-agent's answer within its
  run)

A key added later also reaches accounts that have saved before: it is missing from
their row, so it is read with its default and everything they chose is kept.

#### POST /auth/api-key
Generate a new API key for the current user.

The key is written only while the login it is requested with (token or current
key) is still valid: if a password change comes in between, also from another
process, the endpoint answers 401 and writes nothing.

**Headers:**
```
Authorization: Bearer <token>
```

**Response:**
```json
{
  "api_key": "ak_1234567890abcdef",
  "created_at": "2025-10-10T20:00:00.123456Z",
  "note": "Save this key securely. It will not be shown again."
}
```

#### DELETE /auth/api-key
Revoke the current user's API key.

**Headers:**
```
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "API key revoked successfully"
}
```

### Admin Endpoints

All admin endpoints require the `ADMIN` role.

#### GET /admin/users
List all users with pagination.

**Query Parameters:**
- `skip`: Number of users to skip (default: 0)
- `limit`: Maximum number of users to return (default: 100)

**Response** (`UserListResponse` — `total` is the total number of all users,
not the page size; clients paginate with `skip + limit >= total`):
```json
{
  "users": [
    {
      "id": 1,
      "username": "admin",
      "email": "admin@example.com",
      "full_name": "Administrator",
      "is_active": true,
      "role": "admin",
      "created_at": "2025-10-10T20:00:00.123456Z",
      "last_login": "2025-10-10T20:30:00.123456Z"
    }
  ],
  "total": 1,
  "skip": 0,
  "limit": 100
}
```

#### GET /admin/users/{user_id}
Get a specific user by ID.

#### POST /admin/users
Create a new user (admin operation).

**Request:**
```json
{
  "username": "newuser",
  "email": "new@example.com",
  "password": "SecurePass123!",
  "full_name": "New User",
  "role": "user",
  "is_active": true
}
```

#### PATCH /admin/users/{user_id}
Update user details.

**Request:**
```json
{
  "full_name": "Updated Name",
  "role": "admin"
}
```

#### DELETE /admin/users/{user_id}
Delete a user.

#### POST /admin/users/{user_id}/activate
Activate a user account.

#### POST /admin/users/{user_id}/deactivate
Deactivate a user account.

#### POST /admin/users/{user_id}/promote
Promote user to ADMIN role.

#### POST /admin/users/{user_id}/demote
Demote admin to USER role.

## CLI Usage

### List Users

```bash
agent-cli users list
```

Example output:
```
ID  USERNAME    EMAIL              FULL_NAME       ROLE   ACTIVE
─────────────────────────────────────────────────────────────────
1   admin       admin@example.com  Administrator   ADMIN  ✓
2   johndoe     john@example.com   John Doe        USER   ✓
3   janedoe     jane@example.com   Jane Doe        GUEST  ✗
```

The commands address users by **username** (positional argument), not by
`--email`. Help: `agent-cli users COMMAND --help`. Errors end with exit code 1.

### Create User

```bash
# Interactive (prompts for password securely)
agent-cli users create johndoe john@example.com --name "John Doe" --admin

# With password (not recommended for scripts)
agent-cli users create johndoe john@example.com --password SecurePass123! --name "John Doe"

# As regular user (default role)
agent-cli users create janedoe user@example.com
```

### Delete User

```bash
agent-cli users delete johndoe          # -f skips the confirmation
```

### Update User

```bash
# Update name
agent-cli users update johndoe --name "John Smith"

# Change role
agent-cli users update johndoe --role admin

# Deactivate user
agent-cli users update johndoe --deactivate
```

### Show User Info

```bash
agent-cli users info johndoe
```

Example output:
```
User Information:
─────────────────
ID:         2
Username:   johndoe
Email:      john@example.com
Full Name:  John Doe
Role:       USER
Active:     ✓
Created:    2025-10-10 20:00:00
Last Login: 2025-10-10 20:30:00
```

### API Key Management

```bash
# Generate API key
agent-cli users generate-api-key johndoe

# Revoke API key
agent-cli users revoke-api-key johndoe
```

## Migration Guide

### From Single-User to Multi-User

1. **Backup Your Data**
   ```bash
   # Backup configuration
   cp config/config.yaml config/config.yaml.backup
   
   # Backup any session data
   cp -r data/ data.backup/
   ```

2. **Update Configuration**
   - Edit `config/config.yaml`
   - Set `auth.enabled: true`
   - Configure `auth.secret_key` (generate new secret)

3. **Start the API**
   ```bash
   agent-api
   ```
   The system will automatically create the admin user on first startup.

4. **Change Default Admin Password**
   ```bash
   # First: the change ends every login made with the old password, the API key included
   agent-cli users update admin --password NewSecurePassword

   # Then log in with the new one (and generate an API key, if you need one)
   curl -X POST http://127.0.0.1:8000/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username": "admin", "password": "NewSecurePassword"}'
   ```

5. **Create Additional Users**
   ```bash
   agent-cli users create regular user@example.com --name "Regular User"
   ```

### Backward Compatibility

When `auth.enabled: false` (default):
- All endpoints work without authentication
- No user isolation or access controls
- Single-user mode (existing behavior)

When `auth.enabled: true`:
- Authentication required for most endpoints
- User isolation enforced
- Rate limiting and security headers active
- Admin-only endpoints restricted

### Session Isolation (Future Work)

**Note:** The current implementation provides authentication and user management, but **session isolation is not yet implemented**. Task 9181 tracks this work.

To implement full session isolation:
1. Add `user_id` column to session storage
2. Filter sessions by user in all session endpoints
3. Update `/run` endpoint to associate requests with users
4. Add user context to agent execution
5. Implement session-level access controls

## Database Schema

The user database (`data/users.db`) contains three tables, `users`,
`user_preferences` and `token_generations`:

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT,
    hashed_password TEXT NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    role TEXT DEFAULT 'user',  -- admin, user, guest
    api_key TEXT,  -- SHA-256 hashed
    created_at TEXT NOT NULL,
    updated_at TEXT,
    last_login TEXT
)

CREATE TABLE user_preferences (
    user_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,        -- UserPreferences as JSON
    updated_at TEXT NOT NULL
)

CREATE TABLE token_generations (
    user_id INTEGER PRIMARY KEY,
    generation INTEGER NOT NULL  -- password changes of the account so far
)
```

The database creates `user_preferences` and `token_generations` itself at startup
(also in an existing `users.db`); a row is deleted with its account. A row exists
only once someone has chosen something or changed the password, respectively —
without it, the defaults or generation 0 apply.
**`token_generations` belongs in every copy and every move of the database:**
without it, every account counts as 0 again, and logins from before a password
change are valid again.

### PostgreSQL Migration

To migrate to PostgreSQL:

1. Install psycopg2: `pip install psycopg2-binary`
2. Update database connection in `auth/database.py`
3. Convert SQLite schema to PostgreSQL (adjust types as needed)
4. Migrate user data (`users`, `user_preferences` and `token_generations`)

Example PostgreSQL schema:
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255),
    hashed_password VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    role VARCHAR(50) DEFAULT 'user',
    api_key VARCHAR(255),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    last_login TIMESTAMP
);

CREATE TABLE user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    data JSONB NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE token_generations (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    generation INTEGER NOT NULL
);
```

## Security Considerations

### Password Security
- Passwords hashed with bcrypt (work factor 12)
- Never stored or logged in plain text
- Minimum password complexity enforced by applications

### Token Security
- JWT tokens expire after 30 minutes (configurable)
- Tokens signed with HS256 algorithm
- Secret key must be kept secure

### API Key Security
- API keys hashed with SHA-256 before storage
- Original key shown only once at generation
- Revocation immediately invalidates key

### Rate Limiting
- 60 requests per minute per IP (configurable)
- Protects against brute-force attacks
- Sliding window implementation

### Security Headers
- X-Frame-Options: DENY -- except for the pages the shell shows in frames (`/ui/`, `/plugins/`, `/debug/`), which get SAMEORIGIN
- X-Content-Type-Options: nosniff
- X-XSS-Protection: 1; mode=block
- Strict-Transport-Security (HSTS)
- Content-Security-Policy (CSP)

## Testing

Comprehensive test suite available in `tests/auth/test_auth_system.py`:

```bash
# Run auth system tests
python -m pytest tests/auth/test_auth_system.py -v

# Run all tests
python -m pytest tests/ -v
```

Test coverage includes:
- Password hashing and verification
- JWT token creation, decoding, and expiration
- API key generation, hashing, and verification
- User database CRUD operations
- User role management
- User activation/deactivation
- Duplicate username/email prevention
- API key management

## Troubleshooting

### Common Issues

1. **"Authentication failed" errors**
   - Check that `auth.enabled: true` in config
   - Verify token hasn't expired (30-minute default)
   - Ensure correct Authorization header format

2. **"User not found" errors**
   - Verify user exists: `agent-cli users list`
   - Check username/email spelling
   - Ensure user is active

3. **"Permission denied" errors**
   - Check user role (ADMIN required for admin endpoints)
   - Verify user is active (`is_active: true`)

4. **Database errors**
   - Check `data/users.db` file exists and is writable
   - Verify database path in config
   - Check file permissions

5. **Rate limit errors**
   - Adjust `auth.requests_per_minute` in config
   - Wait for rate limit window to reset (60 seconds)

### Debug Mode

Enable debug logging to troubleshoot authentication issues:

```bash
# Set log level in config
AGENT_LOG_LEVEL=debug agent-api
```

Check logs for:
- JWT token validation errors
- Database connection issues
- Authentication middleware execution
- Rate limiting triggers

## Web UI Integration

### User Management Plugin

The system includes a comprehensive web-based user management interface accessible through the main web UI:

**Features:**
- User dashboard with list view and statistics
- Create, update, and delete users
- Role management (promote/demote users)
- Activate/deactivate user accounts
- User statistics and analytics
- Admin-only access with proper authorization

**Access:**
1. Navigate to `http://127.0.0.1:8000/` (web UI home)
2. Login with admin credentials
3. Open the panel launcher (grid button in the header) or the command palette (Ctrl+K)
4. Choose "Users"

**Plugin Configuration:**

The user management plugin declares its panel in `src/plugins/user_management/schema.yaml`:

```yaml
web_ui:
  panel:
    endpoint: "/plugins/{{ name }}/"
    title: "Users"
    description: "User accounts, roles and permissions"
    icon: users
    category: admin
    keywords: [accounts, roles, permissions]
    window: {width: 960, height: 680}
```

### Panels and Roles

Plugins contribute panels to the web UI. The shell loads the panel catalogue from `GET /api/ui/catalog`: the core panels plus the `web_ui.panel` block of every registered web plugin, filtered by the viewer's role. The launcher, the command palette and the context links in the chat all read this list.

- **Account Menu**: the avatar in the header opens a menu with the user's name and role, Settings, System and Log out
- **Role-Based Filtering**: a plugin panel is listed for the roles that may open its endpoint -- the same rules in `config/security.yaml` (included by `config/config.yaml`) that guard the plugin's routes decide, app-wide `auth.endpoint_security` and `auth.plugin_security` together, so an admin-only route (like the `endpoint_rules` entry for `/plugins/user_management/*`) is an admin-only panel. With authentication disabled the viewer counts as admin and sees every panel

### Authentication Flow in Web UI

1. **Login Page**: `http://127.0.0.1:8000/login`
   - Username/password authentication
   - JWT token stored in an HttpOnly `access_token` cookie
   - Automatic redirect to home page on success

2. **Authenticated Session**:
   - The account menu in the header shows the user's name and role
   - Admin-only panels appear in the launcher and the command palette for admins only
   - API requests from the shell and its panels carry the cookie (same origin)

3. **Logout**:
   - `POST /auth/logout` removes the cookie
   - Redirect to login page

## Future Enhancements

Completed in Epic 0038:
- ✅ **Task 9184**: Web UI for user management
- ✅ **Task 9188**: User management plugin for web UI

Still planned:
- **Task 9181**: Full session isolation per user

Additional ideas:
- OAuth2 integration (Google, GitHub, etc.)
- Two-factor authentication (2FA)
- User groups and granular permissions
- Audit logging for user actions
- Password reset via email
- User session management (view/revoke active sessions)
- IP whitelisting per user
- User quotas and usage tracking

## References

- [FastAPI Security](https://fastapi.tiangolo.com/tutorial/security/)
- [JWT Introduction](https://jwt.io/introduction)
- [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [bcrypt](https://pypi.org/project/bcrypt/)
- [python-jose](https://github.com/mpdavis/python-jose)
