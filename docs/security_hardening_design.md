# Security Hardening Design Document

## Overview

This document describes the security hardening implementation for public internet deployment
of the AgentSystem. The goal is multi-layered security that protects against unauthorized
access while maintaining flexibility for development and private deployments.

## Current State Analysis

### Existing Security Features
1. **JWT Authentication**: Bearer tokens with configurable expiration
2. **API Key Support**: X-API-Key header for programmatic access
3. **User Roles**: ADMIN, USER, GUEST (defined but not fully enforced)
4. **Rate Limiting**: Per-IP request limiting (optional)
5. **Security Headers**: X-Frame-Options, CSP, HSTS, etc.
6. **CORS Configuration**: Configurable origins

### Current Gaps
1. **Auth Optional**: `auth.enabled=true` doesn't enforce auth on all endpoints
2. **Anonymous Access**: No clear policy - some endpoints work without auth
3. **No LLM Request Validation**: Agents can make LLM calls without verifying user context
4. **Endpoint Inconsistency**: Mix of protected and unprotected endpoints
5. **No Resource-Level Authorization**: Users can access any session/data

## Security Architecture

### 1. Authentication Layers

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
│                    LAYER 5: Request Context                      │
│        User context propagation through agent execution          │
└─────────────────────────────────────────────────────────────────┘
```

### 2. Configuration Schema

New `auth` configuration fields in `config.yaml`:

```yaml
auth:
  enabled: true                     # Master switch for authentication
  
  # Anonymous Access Control
  anonymous_access:
    enabled: false                  # Allow unauthenticated access
    role: "guest"                   # Role assigned to anonymous users
    allowed_endpoints:              # Endpoints accessible without auth
      - "GET /health"
      - "GET /agents"               # Read-only agent list
      - "GET /static/*"             # Static files
      - "GET /login"                # Login page
      - "POST /auth/login"          # Login endpoint
    rate_limit_multiplier: 0.5      # Stricter rate limit for anonymous (50%)
  
  # Endpoint Protection Rules
  endpoint_security:
    # Default policy when auth.enabled=true
    default_policy: "require_auth"  # "require_auth" | "allow_anonymous"
    
    # Override rules (processed in order)
    rules:
      - pattern: "POST /run"
        policy: "require_auth"
        min_role: "user"
      - pattern: "GET /events"
        policy: "require_auth"
        min_role: "user"
      - pattern: "/admin/*"
        policy: "require_auth"
        min_role: "admin"
      - pattern: "/sessions/*"
        policy: "require_auth"
        min_role: "user"
  
  # LLM Request Security
  llm_security:
    require_valid_user: true        # LLM calls require authenticated user
    validate_session_ownership: true # Users can only use their own sessions
    audit_llm_requests: true        # Log all LLM requests with user info
```

### 3. Endpoint Security Matrix

| Endpoint              | Auth Disabled | Auth + Anonymous | Auth Required |
|-----------------------|---------------|------------------|---------------|
| GET /health           | ✅ Open       | ✅ Open          | ✅ Open       |
| GET /login            | ✅ Open       | ✅ Open          | ✅ Open       |
| POST /auth/login      | ✅ Open       | ✅ Open          | ✅ Open       |
| POST /auth/register   | ✅ Open       | ⚙️ auth.registration | ⚙️ auth.registration |
| GET /agents           | ✅ Open       | ⚙️ Configurable  | 🔐 User+      |
| GET /config           | ✅ Open       | 🔐 User+         | 🔐 User+      |
| POST /run             | ✅ Open       | 🔐 User+         | 🔐 User+      |
| GET /events           | ✅ Open       | 🔐 User+         | 🔐 User+      |
| POST /sessions/*      | ✅ Open       | 🔐 User+         | 🔐 User+      |
| /admin/*              | ✅ Open       | 🔐 Admin only    | 🔐 Admin only |
| GET /static/*         | ✅ Open       | ✅ Open          | ✅ Open       |

Legend: ✅ Open = No auth needed, 🔐 = Requires authentication, ⚙️ = Configurable

### 4. Implementation Plan

#### Phase 1: Configuration Model Extensions
- Add `anonymous_access` to AuthConfig
- Add `endpoint_security` rules
- Add `llm_security` settings

#### Phase 2: Authentication Middleware Enhancement
- Create `EnforcedAuthMiddleware` that respects config
- Implement endpoint pattern matching
- Support anonymous user creation with limited role

#### Phase 3: Endpoint Protection
- Wrapper dependency `require_auth_for_endpoint`
- Role validation based on endpoint rules
- Session ownership validation

#### Phase 4: LLM Request Context
- Pass user context to Agent execution
- Validate user in LLM request chain
- Audit logging for LLM requests

#### Phase 5: Resource Authorization
- Session ownership validation
- User-scoped session listing
- Cross-user access prevention

## Detailed Implementation

### 4.1 Configuration Model Changes

```python
# In config/models.py

class AnonymousAccessConfig(BaseModel):
    """Configuration for anonymous (unauthenticated) access."""
    enabled: bool = False
    role: str = "guest"  # Role assigned to anonymous users
    allowed_endpoints: List[str] = Field(default_factory=lambda: [
        "GET /health",
        "GET /static/*",
        "GET /login",
        "POST /auth/login"
    ])
    rate_limit_multiplier: float = 0.5  # Stricter for anonymous


class EndpointRule(BaseModel):
    """Security rule for an endpoint pattern."""
    pattern: str  # e.g., "POST /run", "/admin/*", "GET /sessions/*"
    policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    min_role: Optional[str] = None  # "admin", "user", "guest"


class EndpointSecurityConfig(BaseModel):
    """Endpoint-level security configuration."""
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    rules: List[EndpointRule] = Field(default_factory=list)


class LLMSecurityConfig(BaseModel):
    """Security configuration for LLM requests."""
    require_valid_user: bool = True
    validate_session_ownership: bool = True
    audit_llm_requests: bool = True


class AuthConfig(BaseModel):
    # ... existing fields ...
    
    # New security fields
    anonymous_access: AnonymousAccessConfig = Field(default_factory=AnonymousAccessConfig)
    endpoint_security: EndpointSecurityConfig = Field(default_factory=EndpointSecurityConfig)
    llm_security: LLMSecurityConfig = Field(default_factory=LLMSecurityConfig)
```

### 4.2 Enforced Authentication Middleware

```python
# In auth/middleware.py

class EnforcedAuthMiddleware:
    """
    ASGI middleware that enforces authentication based on config.
    
    When auth.enabled=true:
    - Checks each request against endpoint_security rules
    - Returns 401 for unauthenticated requests to protected endpoints
    - Allows anonymous access only when explicitly configured
    """
    
    def __init__(self, app: ASGIApp, config: AuthConfig):
        self.app = app
        self.config = config
        self._compiled_rules = self._compile_rules()
    
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Skip if auth disabled
        if not self.config.enabled:
            await self.app(scope, receive, send)
            return
        
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        
        # Check if endpoint requires auth
        requires_auth, min_role = self._get_endpoint_policy(method, path)
        
        if not requires_auth:
            await self.app(scope, receive, send)
            return
        
        # Extract user from request (JWT/API key)
        user = await self._extract_user(scope)
        
        if user is None:
            if self.config.anonymous_access.enabled:
                # Create anonymous user with guest role
                scope["user"] = AnonymousUser(role=self.config.anonymous_access.role)
            else:
                # Return 401
                await self._send_unauthorized(send)
                return
        else:
            scope["user"] = user
        
        # Check role if required
        if min_role and not self._has_role(scope.get("user"), min_role):
            await self._send_forbidden(send)
            return
        
        await self.app(scope, receive, send)
```

### 4.3 User Context in Agent Execution

```python
# In servers/agent/server.py - Agent.execute()

async def execute(
    self,
    task: str,
    *,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_context: Optional[UserContext] = None,  # NEW
    **kwargs
) -> AgentResult:
    """Execute agent with user context validation."""
    
    # Validate user context for LLM requests
    if self._config.llm_security.require_valid_user:
        if user_context is None or not user_context.is_authenticated:
            raise PermissionError("LLM requests require authenticated user")
    
    # Validate session ownership
    if session_id and self._config.llm_security.validate_session_ownership:
        session_owner = self._session_tracker.get_session_owner(session_id)
        if session_owner and session_owner != user_context.user_id:
            raise PermissionError(f"Session {session_id} belongs to another user")
    
    # ... rest of execution ...
```

### 4.4 Audit Logging

```python
# In llm/factory.py or dedicated audit module

class LLMAuditLogger:
    """Audit logger for LLM requests."""
    
    def log_request(
        self,
        user_id: str,
        model: str,
        request_type: str,  # "chat", "completion", "tool_call"
        token_estimate: int,
        session_id: Optional[str] = None,
    ):
        audit_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "model": model,
            "request_type": request_type,
            "token_estimate": token_estimate,
            "session_id": session_id,
        }
        # Log to dedicated audit log
        audit_logger.info(json.dumps(audit_entry))
```

## Security Checklist

### Authentication
- [ ] All endpoints respect `auth.enabled` setting
- [ ] Anonymous access controlled by explicit config
- [ ] JWT tokens validated on every protected request
- [ ] API keys hashed and validated securely
- [ ] Token expiration enforced

### Authorization
- [ ] Role hierarchy enforced (ADMIN > USER > GUEST)
- [ ] Endpoint-level role requirements
- [ ] Session ownership validation
- [ ] Cross-user data access prevented

### Rate Limiting
- [ ] Per-IP rate limiting active
- [ ] Stricter limits for anonymous users
- [ ] Rate limit bypass prevention

### LLM Security
- [ ] User context required for LLM calls
- [ ] Session ownership validated
- [ ] Audit logging enabled

### Transport Security
- [ ] HTTPS recommended in production docs
- [ ] Security headers enabled
- [ ] CORS properly configured

## Migration Guide

### From Open Deployment to Secured

1. Enable authentication:
   ```yaml
   auth:
     enabled: true
   ```

2. Disable anonymous access (default):
   ```yaml
   auth:
     anonymous_access:
       enabled: false
   ```

3. Configure endpoint rules if custom behavior needed

4. Restart server and verify access

### Allowing Read-Only Anonymous Access

```yaml
auth:
  enabled: true
  anonymous_access:
    enabled: true
    role: "guest"
    allowed_endpoints:
      - "GET /health"
      - "GET /agents"
      - "GET /static/*"
      - "GET /login"
      - "POST /auth/login"
```

## Testing Strategy

1. **Unit Tests**: Test each security component in isolation
2. **Integration Tests**: Test full request flow with auth
3. **Penetration Tests**: Attempt bypass scenarios
4. **Load Tests**: Verify rate limiting under load

## Future Enhancements

1. **OAuth2/OIDC Support**: External identity providers
2. **API Scopes**: Fine-grained permission control
3. **Audit Dashboard**: Real-time security monitoring
4. **IP Allowlisting**: Additional network-level control
5. **2FA Support**: Multi-factor authentication

## Quick Deployment Guide for Internet Exposure

### Minimal Secure Configuration

For public internet deployment, use this minimal secure configuration:

```yaml
auth:
  enabled: true
  secret_key: "YOUR_VERY_LONG_RANDOM_SECRET_KEY_HERE"  # CHANGE THIS!
  
  # Rate limiting - essential for public deployment
  rate_limit_enabled: true
  requests_per_minute: 30  # Adjust based on expected load
  
  # Security headers
  security_headers_enabled: true
  
  # CORS - restrict to your domain
  cors_enabled: true
  cors_origins:
    - "https://yourdomain.com"
  cors_credentials: true
  
  # Admin user - change immediately after first login
  default_admin_username: "admin"
  default_admin_password: null  # Will be auto-generated, check logs
  
  # Disable anonymous access for maximum security
  anonymous_access:
    enabled: false
  
  # Strict endpoint security
  endpoint_security:
    default_policy: "require_auth"
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
  
  # LLM security - prevent cost abuse
  llm_security:
    require_valid_user: true
    validate_session_ownership: true
    audit_llm_requests: true
    max_requests_per_hour_anonymous: 0  # Block anonymous LLM requests

  # Plugin endpoint security - centralized control
  plugin_security:
    default_policy: "require_auth"
    default_min_role: "user"
    audit_enabled: true
    endpoint_rules:
      - pattern: "/plugins/*/admin/*"
        policy: "require_auth"
        min_role: "admin"
```

## Plugin Endpoint Security

### Overview

All plugin-provided HTTP endpoints are centrally controlled via `auth.plugin_security`:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Plugin Endpoint Security                      │
├─────────────────────────────────────────────────────────────────┤
│  1. Endpoint Pattern Rules (highest priority)                   │
│     - Match specific paths like "/plugins/todo/admin/*"         │
│     - Support wildcards and HTTP method prefixes                │
├─────────────────────────────────────────────────────────────────┤
│  2. Plugin-Specific Overrides                                   │
│     - Per-plugin policy and role configuration                  │
│     - Example: {"todo": {"policy": "allow_anonymous"}}          │
├─────────────────────────────────────────────────────────────────┤
│  3. Global Default Policy                                       │
│     - default_policy: "require_auth" | "allow_anonymous"        │
│     - default_min_role: "user" | "admin" | "guest"              │
└─────────────────────────────────────────────────────────────────┘
```

### Configuration Options

```yaml
auth:
  plugin_security:
    # Global defaults
    default_policy: "require_auth"    # Default for all plugin endpoints
    default_min_role: "user"          # Minimum role required
    audit_enabled: true               # Log all plugin endpoint access
    
    # Plugin-specific overrides
    plugin_overrides:
      public_plugin:
        policy: "allow_anonymous"     # Allow anonymous access
      admin_tools:
        min_role: "admin"             # Require admin role
    
    # Endpoint pattern rules (processed in order)
    endpoint_rules:
      - pattern: "/plugins/*/admin/*"
        policy: "require_auth"
        min_role: "admin"
        description: "Plugin admin endpoints"
      
      - pattern: "GET /plugins/todo/panel"
        policy: "allow_anonymous"
        description: "Public todo panel"
```

### Audit Logging

When `auth.endpoint_security.audit_enabled: true`, every request (static files aside) is logged to
`logs/security.log` by the app-wide audit; a plugin request the plugin rules refuse gets a second line
with the rule's reason:

```
2026-09-15 10:00:00 | INFO | ALLOWED | POST /plugins/todo/tasks | user=john | ip=10.0.0.5 | status=200 | 12.3ms | plugin
2026-09-15 10:00:01 | WARNING | DENIED | GET /plugins/admin_tools/users | plugin=admin_tools | user=anonymous | Authentication required but no user found
```

### Monitoring Endpoints

Two admin endpoints are available for monitoring:

1. **GET /api/plugins/security/status** - Current security configuration (admin only)
2. **GET /admin/security/audit** - The newest requests the app-wide audit kept in memory, newest first
   (admin only, 404 while the audit is off; `category`, repeatable `status=4xx`, `limit` 1-1000).
   The **Security Audit** panel (`/ui/panels/security_audit`) shows it. Refused plugin requests
   additionally land in `logs/security.log` with the plugin rule's reason.

Example response from `/api/plugins/security/status`:
```json
{
  "auth_enabled": true,
  "plugin_security_enabled": true,
  "default_policy": "require_auth",
  "default_min_role": "user",
  "audit_enabled": true,
  "plugins": {
    "todo": {
      "has_router": true,
      "has_static": true,
      "security_policy": {
        "requires_auth": true,
        "min_role": "user"
      }
    }
  }
}
```

### Production Checklist

Before going live:

1. **[ ] Generate strong secret_key** (at least 32 random characters)
2. **[ ] Set up HTTPS** via reverse proxy (nginx, Caddy, etc.)
3. **[ ] Configure CORS origins** to your specific domain
4. **[ ] Enable rate limiting** with appropriate limits
5. **[ ] Change default admin password** immediately after first login
6. **[ ] Review endpoint rules** for your use case
7. **[ ] Set up log rotation** for audit logs
8. **[ ] Configure firewall** to only expose port 443 (HTTPS)
9. **[ ] Test all endpoints** with and without authentication
10. **[ ] Set up monitoring/alerting** for failed auth attempts
