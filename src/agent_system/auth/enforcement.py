"""
Endpoint Security Enforcement

Provides centralized security enforcement for API endpoints based on
configuration rules. This module handles:
- Endpoint pattern matching against security rules
- Role-based access control validation
- Anonymous user handling
- User context propagation

Usage:
    from agent_system.auth.enforcement import EndpointSecurityEnforcer
    
    enforcer = EndpointSecurityEnforcer(auth_config)
    policy = enforcer.get_endpoint_policy("POST", "/run")
    if policy.requires_auth:
        user = await enforcer.validate_request(request)
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status

if TYPE_CHECKING:
    from agent_system.config.models import AuthConfig, EndpointSecurityRule


logger = logging.getLogger(__name__)


# Role hierarchy: higher value = more permissions
ROLE_HIERARCHY = {
    "guest": 1,
    "user": 2,
    "admin": 3,
}


#: The methods a rule or an allowed endpoint may name in front of its path.
_PATTERN_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "*")


def split_method_prefix(pattern: str) -> tuple[str, str]:
    """``(method, path_pattern)`` of an endpoint pattern such as "POST /run".

    Without a leading method this reads as ``("*", pattern)``; so does a
    leading word that is not one of _PATTERN_METHODS.
    """
    method = "*"
    path_pattern = pattern

    parts = pattern.split(" ", 1)
    if len(parts) == 2 and parts[0].upper() in _PATTERN_METHODS:
        method = parts[0].upper()
        path_pattern = parts[1]
    return method, path_pattern


def compile_endpoint_rules(
    rules: "list[EndpointSecurityRule]",
) -> list[tuple[re.Pattern, str, "EndpointSecurityRule"]]:
    """Compile endpoint patterns into regex for efficient matching.

    One ``(regex, method, rule)`` per rule, in config order. Both readers of
    ``endpoint_security.rules`` -- EndpointSecurityEnforcer here and the
    EndpointSecurityMiddleware in auth/middleware.py -- compile through this,
    so the two layers cannot read one rule two ways again.
    """
    compiled_rules: list[tuple[re.Pattern, str, "EndpointSecurityRule"]] = []

    for rule in rules:
        pattern = rule.pattern.strip()

        # Parse method prefix if present (e.g., "POST /run")
        method, path_pattern = split_method_prefix(pattern)

        # Convert glob pattern to regex
        regex_pattern = fnmatch.translate(path_pattern)

        # Only remove \Z for wildcard patterns (to allow prefix matching).
        # For exact patterns keep \Z — unconditional removal degraded
        # every exact rule to a prefix match ("GET /" matched ALL paths).
        # auth/middleware.py fixed this first; the enforcer had its own copy
        # of this loop until both compiled through here.
        if '*' in path_pattern or '?' in path_pattern:
            regex_pattern = regex_pattern.replace(r'\Z', '')

        try:
            compiled = re.compile(regex_pattern, re.IGNORECASE)
            compiled_rules.append((compiled, method, rule))
        except re.error as e:
            logger.warning(f"Invalid pattern '{pattern}': {e}")
    return compiled_rules


def first_matching_rule(
    compiled_rules: list[tuple[re.Pattern, str, "EndpointSecurityRule"]],
    method: str,
    path: str,
) -> Optional["EndpointSecurityRule"]:
    """The first rule, in config order, whose method and path match; None if none does.

    ``method`` is expected upper-case.
    """
    for compiled_pattern, rule_method, rule in compiled_rules:
        # Check method match
        if rule_method != "*" and rule_method != method:
            continue

        # Check path match
        if compiled_pattern.match(path):
            return rule
    return None


@dataclass
class EndpointPolicy:
    """Security policy for an endpoint."""
    requires_auth: bool
    min_role: Optional[str]
    rule_description: Optional[str]
    matched_pattern: Optional[str]


@dataclass
class AnonymousUser:
    """Represents an anonymous (unauthenticated) user.
    
    Used when anonymous_access is enabled to provide a consistent
    user object with limited permissions.
    """
    username: str = "anonymous"
    role: str = "guest"
    is_active: bool = True
    is_authenticated: bool = False
    id: Optional[int] = None
    
    @property
    def user_id(self) -> str:
        """Return user identifier for session tracking."""
        return "anonymous"


@dataclass
class UserContext:
    """User context for request processing.
    
    Contains user information and authentication status for
    propagation through the request lifecycle.
    """
    user_id: str
    username: str
    role: str
    is_authenticated: bool
    is_anonymous: bool = False
    
    @classmethod
    def from_user(cls, user: Any) -> "UserContext":
        """Create UserContext from a User or AnonymousUser object."""
        if isinstance(user, AnonymousUser):
            return cls(
                user_id="anonymous",
                username="anonymous",
                role=user.role,
                is_authenticated=False,
                is_anonymous=True,
            )
        else:
            return cls(
                user_id=str(user.id) if hasattr(user, 'id') else user.username,
                username=user.username,
                role=user.role if hasattr(user, 'role') else "user",
                is_authenticated=True,
                is_anonymous=False,
            )


class EndpointSecurityEnforcer:
    """
    Enforces endpoint security based on configuration rules.
    
    This class provides centralized security enforcement by:
    1. Matching request paths against configured rules
    2. Determining authentication requirements
    3. Validating user roles
    4. Handling anonymous access when permitted
    
    Example:
        enforcer = EndpointSecurityEnforcer(config.auth)
        policy = enforcer.get_endpoint_policy("POST", "/run")
        
        if policy.requires_auth:
            user = await get_current_user(request)
            enforcer.validate_role(user, policy.min_role)
    """
    
    def __init__(self, auth_config: "AuthConfig"):
        """Initialize the enforcer with auth configuration.
        
        Args:
            auth_config: Authentication configuration from config.yaml
        """
        self.config = auth_config
        self._compiled_patterns: list[tuple[re.Pattern, str, "EndpointSecurityRule"]] = []
        self._compile_patterns()
    
    def _compile_patterns(self) -> None:
        """Compile endpoint patterns into regex for efficient matching."""
        self._compiled_patterns = compile_endpoint_rules(self.config.endpoint_security.rules)
    
    def get_endpoint_policy(self, method: str, path: str) -> EndpointPolicy:
        """Get the security policy for an endpoint.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path (e.g., "/run", "/admin/users")
        
        Returns:
            EndpointPolicy with authentication requirements
        """
        # Check if auth is disabled entirely
        if not self.config.enabled:
            return EndpointPolicy(
                requires_auth=False,
                min_role=None,
                rule_description="Authentication disabled",
                matched_pattern=None,
            )
        
        method = method.upper()
        
        # Check configured rules in order
        rule = first_matching_rule(self._compiled_patterns, method, path)
        if rule is not None:
            requires_auth = rule.policy == "require_auth"
            return EndpointPolicy(
                requires_auth=requires_auth,
                min_role=rule.min_role if requires_auth else None,
                rule_description=rule.description,
                matched_pattern=rule.pattern,
            )
        
        # No rule matched, use default policy
        default_requires_auth = self.config.endpoint_security.default_policy == "require_auth"
        return EndpointPolicy(
            requires_auth=default_requires_auth,
            min_role="user" if default_requires_auth else None,
            rule_description="Default policy",
            matched_pattern=None,
        )
    
    def is_endpoint_allowed_anonymous(self, method: str, path: str) -> bool:
        """Check if an endpoint allows anonymous access.
        
        This checks the anonymous_access.allowed_endpoints list
        for endpoints that should be accessible without authentication.
        
        Args:
            method: HTTP method
            path: Request path
        
        Returns:
            True if anonymous access is allowed for this endpoint
        """
        if not self.config.anonymous_access.enabled:
            return False
        
        method = method.upper()
        
        for allowed in self.config.anonymous_access.allowed_endpoints:
            allowed = allowed.strip()
            
            # Parse method prefix
            allowed_method, allowed_path = split_method_prefix(allowed)
            
            # Check method
            if allowed_method != "*" and allowed_method != method:
                continue
            
            # Check path with glob matching
            if fnmatch.fnmatch(path, allowed_path):
                return True
        
        return False
    
    def validate_role(self, user: Any, min_role: Optional[str]) -> None:
        """Validate that a user has the required role.
        
        Args:
            user: User object (User or AnonymousUser)
            min_role: Minimum required role (None = any authenticated user)
        
        Raises:
            HTTPException: 403 if user lacks required role
        """
        if min_role is None:
            return
        
        user_role = getattr(user, 'role', 'guest')
        if isinstance(user_role, str):
            user_role_value = ROLE_HIERARCHY.get(user_role.lower(), 0)
        else:
            # Handle enum
            user_role_value = ROLE_HIERARCHY.get(str(user_role.value).lower(), 0)
        
        required_role_value = ROLE_HIERARCHY.get(min_role.lower(), 0)
        
        if user_role_value < required_role_value:
            logger.warning(
                f"Access denied: user role '{user_role}' < required '{min_role}'"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {min_role}",
            )
    
    def create_anonymous_user(self) -> AnonymousUser:
        """Create an anonymous user with configured guest role.
        
        Returns:
            AnonymousUser with role from anonymous_access.role config
        """
        return AnonymousUser(role=self.config.anonymous_access.role)
    
    async def enforce_endpoint_security(
        self,
        request: Request,
        get_user_func,
    ) -> Optional[Any]:
        """Enforce security for a request and return the user.
        
        This is the main entry point for security enforcement.
        It checks the endpoint policy, validates authentication,
        and returns the user (or AnonymousUser if permitted).
        
        Args:
            request: FastAPI Request object
            get_user_func: Async function to get current user from request
        
        Returns:
            User object (User or AnonymousUser)
        
        Raises:
            HTTPException: 401 if authentication required but not provided
            HTTPException: 403 if user lacks required role
        """
        method = request.method
        path = request.url.path
        
        # Get policy for this endpoint
        policy = self.get_endpoint_policy(method, path)
        
        logger.debug(
            f"[SECURITY] {method} {path} - requires_auth={policy.requires_auth}, "
            f"min_role={policy.min_role}, pattern={policy.matched_pattern}"
        )
        
        # If auth not required, still try to get user for context
        if not policy.requires_auth:
            try:
                user = await get_user_func(request)
                if user:
                    return user
            except Exception:
                pass
            
            # No user and not required - allow access
            if self.config.anonymous_access.enabled:
                return self.create_anonymous_user()
            return None
        
        # Auth required - try to get user
        user = None
        try:
            user = await get_user_func(request)
        except HTTPException:
            # Auth failed
            pass
        except Exception as e:
            logger.debug(f"[SECURITY] Error getting user: {e}")
        
        if user is not None:
            # User authenticated - validate role
            self.validate_role(user, policy.min_role)
            return user
        
        # No authenticated user - check if anonymous allowed
        if self.is_endpoint_allowed_anonymous(method, path):
            return self.create_anonymous_user()
        
        # Not allowed - raise 401
        logger.info(f"[SECURITY] Unauthorized access attempt: {method} {path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def has_role(user: Any, required_role: str) -> bool:
    """Check if a user has at least the required role.
    
    Args:
        user: User object
        required_role: Required role name
    
    Returns:
        True if user has required role or higher
    """
    if user is None:
        return False
    
    user_role = getattr(user, 'role', 'guest')
    if hasattr(user_role, 'value'):
        user_role = user_role.value
    
    user_level = ROLE_HIERARCHY.get(str(user_role).lower(), 0)
    required_level = ROLE_HIERARCHY.get(required_role.lower(), 0)
    
    return user_level >= required_level
