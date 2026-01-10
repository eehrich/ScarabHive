"""
Authentication and Authorization Module

This module provides comprehensive multi-user authentication and authorization
for the AgentSystem API. It includes:

- User management (database models, CRUD operations)
- Authentication (JWT tokens, API keys, password hashing)
- Authorization (role-based access control, permissions)
- Session management (user-specific sessions, isolation)
- Security features (rate limiting, CORS, security headers)
- Endpoint security enforcement (pattern-based rules, role validation)

Components:
    - models: User database models and schemas
    - database: Database connection and storage layer
    - security: Password hashing, token generation, validation
    - dependencies: FastAPI dependency injection for auth
    - middleware: Authentication and security middleware
    - enforcement: Centralized endpoint security enforcement (NEW)
"""

from agent_system.auth.models import User, UserRole, UserInDB
from agent_system.auth.database import get_db, create_user, get_user_by_username, get_user_by_email
from agent_system.auth.security import verify_password, get_password_hash, create_access_token, decode_access_token
from agent_system.auth.dependencies import get_current_user, get_current_active_user, require_admin
from agent_system.auth.enforcement import (
    EndpointSecurityEnforcer,
    EndpointPolicy,
    AnonymousUser,
    UserContext,
    has_role,
)

__all__ = [
    "User",
    "UserRole",
    "UserInDB",
    "get_db",
    "create_user",
    "get_user_by_username",
    "get_user_by_email",
    "verify_password",
    "get_password_hash",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "get_current_active_user",
    "require_admin",
    # Security enforcement
    "EndpointSecurityEnforcer",
    "EndpointPolicy",
    "AnonymousUser",
    "UserContext",
    "has_role",
]
