"""The ``auth:`` section: accounts and tokens, self-registration, anonymous access, and the route
rules (endpoint, LLM and plugin security).

Only the master config and the local layer set this section; an include the master names by its
path may set the route rules alone (config/layers.py: MASTER_ONLY_SECTIONS, AUTH_RULE_SECTIONS).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional, Dict, List, Any


# ===========================
# Security Configuration Models
# ===========================

class AnonymousAccessConfig(BaseModel):
    """Configuration for anonymous (unauthenticated) access.
    
    When auth.enabled=true but anonymous_access.enabled=true,
    unauthenticated users can access certain endpoints with
    a limited guest role.
    """
    enabled: bool = False  # Allow unauthenticated access to certain endpoints
    role: str = "guest"  # Role assigned to anonymous users
    allowed_endpoints: List[str] = Field(default_factory=lambda: [
        "GET /health",
        "GET /static/*",
        "GET /login",
        "POST /auth/login",
        "POST /auth/register",  # Allow self-registration when enabled
    ])
    rate_limit_multiplier: float = 0.5  # Stricter rate limit for anonymous (50% of normal)


class EndpointSecurityRule(BaseModel):
    """Security rule for an endpoint pattern.
    
    Patterns support:
    - Exact match: "GET /agents"
    - Wildcard: "/admin/*", "* /sessions/*"
    - Method prefix: "POST /run", "GET /events"
    """
    pattern: str  # e.g., "POST /run", "/admin/*", "GET /sessions/*"
    policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    min_role: Optional[str] = None  # "admin", "user", "guest"
    description: Optional[str] = None  # Human-readable description


class EndpointSecurityConfig(BaseModel):
    """Endpoint-level security configuration.
    
    When auth.enabled=true, this config controls which endpoints
    require authentication and what role is needed.
    """
    # Default policy when no specific rule matches
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    
    # Audit logging for all endpoint access (logs to security.log)
    audit_enabled: bool = True
    
    # Custom rules (processed in order, first match wins)
    rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Admin endpoints always require admin role
        EndpointSecurityRule(
            pattern="/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Admin endpoints require admin role"
        ),
        # Agent execution requires user role
        EndpointSecurityRule(
            pattern="POST /run",
            policy="require_auth",
            min_role="user",
            description="Agent execution requires authentication"
        ),
        # Events stream requires user role
        EndpointSecurityRule(
            pattern="GET /events",
            policy="require_auth",
            min_role="user",
            description="Event stream requires authentication"
        ),
        # Session management requires user role
        EndpointSecurityRule(
            pattern="* /sessions/*",
            policy="require_auth",
            min_role="user",
            description="Session management requires authentication"
        ),
    ])


class LLMSecurityConfig(BaseModel):
    """Security configuration for LLM API calls.
    
    These settings ensure that LLM requests (which cost money)
    are properly authorized and audited.
    """
    require_valid_user: bool = True  # LLM calls require authenticated user context
    validate_session_ownership: bool = True  # Users can only access their own sessions
    audit_llm_requests: bool = True  # Log all LLM requests with user info
    max_tokens_per_request_anonymous: Optional[int] = None  # Token limit for anonymous (None = blocked)
    max_requests_per_hour_anonymous: int = 0  # Hourly limit for anonymous users (0 = blocked)


class PluginSecurityConfig(BaseModel):
    """Security configuration for plugin web endpoints.
    
    Controls how plugin-provided HTTP endpoints are secured.
    By default, all plugin endpoints require authentication.
    """
    # Global default for all plugin endpoints
    default_policy: Literal["require_auth", "allow_anonymous"] = "require_auth"
    default_min_role: str = "user"  # Default minimum role for plugin endpoints
    
    # Plugin-specific overrides (plugin_name -> config)
    # Example: {"todo": {"policy": "allow_anonymous"}, "admin_tools": {"min_role": "admin"}}
    plugin_overrides: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    # Endpoint pattern overrides (same format as endpoint_security.rules)
    # These take precedence over plugin_overrides
    endpoint_rules: List[EndpointSecurityRule] = Field(default_factory=lambda: [
        # Example: Block all plugin admin endpoints for non-admins
        EndpointSecurityRule(
            pattern="/plugins/*/admin/*",
            policy="require_auth",
            min_role="admin",
            description="Plugin admin endpoints require admin role"
        ),
    ])


class RegistrationConfig(BaseModel):
    """Self-registration through POST /auth/register, which is reachable without login.

    The defaults keep what the endpoint always did: open, the account active at once,
    role user."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # New accounts start inactive until an admin activates them (the user management
    # panel, POST /admin/users/{id}/activate, agent-cli users update NAME --activate).
    require_approval: bool = False
    # Never admin: whoever reaches the endpoint chooses nothing about their privileges.
    default_role: Literal["guest", "user"] = "user"


class AuthConfig(BaseModel):
    """Authentication and authorization configuration.
    
    Multi-layered security configuration for the AgentSystem.
    
    Security Layers:
    1. Transport: HTTPS (handled externally by reverse proxy)
    2. Rate Limiting: Per-IP request limiting
    3. Authentication: JWT tokens / API keys
    4. Authorization: Role-based access control
    5. Request Context: User context in agent execution
    """
    enabled: bool = False  # Master switch for authentication system
    secret_key: str = "CHANGE_THIS_SECRET_KEY_IN_PRODUCTION"
    # A published signing key (this default, the development key the repository ships) logs an
    # error at startup; true refuses to start with it. Empty and short keys are always refused.
    reject_default_secret_key: bool = False
    registration: RegistrationConfig = Field(default_factory=RegistrationConfig)
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30  # Refresh token valid for 30 days

    # Database settings
    database_path: str = "data/users.db"

    # Security settings
    rate_limit_enabled: bool = True
    requests_per_minute: int = 60
    security_headers_enabled: bool = True

    # CORS settings
    # cors_credentials defaults to False because cors_origins defaults to the
    # wildcard, and the two are mutually exclusive (configure_cors would drop
    # credentials and warn). Enable it together with an explicit origin
    # allowlist for a cross-origin frontend; the bundled same-origin UI never
    # needs it.
    cors_enabled: bool = True
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    cors_credentials: bool = False
    cors_methods: List[str] = Field(default_factory=lambda: ["*"])
    cors_headers: List[str] = Field(default_factory=lambda: ["*"])

    # Trusted hosts (optional)
    trusted_hosts: Optional[List[str]] = None

    # Default admin user (created on first startup if no users exist)
    default_admin_username: str = "admin"
    default_admin_password: Optional[str] = None  # Generated randomly if not set
    default_admin_email: str = "admin@example.com"  # an EmailStr: "admin@localhost" failed it, and no admin was created
    
    # NEW: Anonymous access configuration
    anonymous_access: AnonymousAccessConfig = Field(default_factory=AnonymousAccessConfig)
    
    # NEW: Endpoint-level security rules
    endpoint_security: EndpointSecurityConfig = Field(default_factory=EndpointSecurityConfig)
    
    # NEW: LLM request security
    llm_security: LLMSecurityConfig = Field(default_factory=LLMSecurityConfig)
    
    # NEW: Plugin endpoint security
    plugin_security: PluginSecurityConfig = Field(default_factory=PluginSecurityConfig)
