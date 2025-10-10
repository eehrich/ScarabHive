"""User Management Web Endpoints

Provides web UI endpoints for user administration and management.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, TYPE_CHECKING, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from agent_system.plugins.web_adapter import PluginWebInterface

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


# Pydantic models for request bodies
class CreateUserRequest(BaseModel):
    """Request model for creating a new user"""
    username: str
    email: str
    password: str
    full_name: Optional[str] = None
    role: str = "user"
    is_active: bool = True


class UpdateUserRequest(BaseModel):
    """Request model for updating an existing user"""
    username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class UserManagementWebEndpoints(PluginWebInterface):
    """Web endpoints component for user management plugin"""
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig"):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        
        # Check if auth is enabled
        self.auth_enabled = getattr(system_config.auth, 'enabled', False) if hasattr(system_config, 'auth') and system_config.auth else False
        
        # Configuration
        self.items_per_page = getattr(mcp_config, 'items_per_page', 20)
        self.allow_self_delete = getattr(mcp_config, 'allow_self_delete', False)
        self.show_api_keys = getattr(mcp_config, 'show_api_keys', True)
        
        # Initialize templates
        template_dir = Path(__file__).parent / "templates"
        self.templates = Jinja2Templates(directory=str(template_dir))
        
        logger.info(f"UserManagementWebEndpoints initialized: {name}, auth_enabled={self.auth_enabled}")
    
    def _get_user_database(self):
        """Get the user database instance"""
        if not self.auth_enabled:
            raise HTTPException(status_code=503, detail="Authentication is not enabled")
        
        try:
            from agent_system.auth.database import get_db
            return get_db()
        except Exception as e:
            logger.error(f"Failed to get user database: {e}")
            raise HTTPException(status_code=500, detail="User database not available")
    
    def _check_admin_permission(self, request: Request):
        """Check if current user has admin permissions"""
        # This is a simplified check - in production, integrate with auth dependencies
        # For now, we'll allow access if auth is enabled (admin endpoints are protected by FastAPI dependencies)
        if not self.auth_enabled:
            raise HTTPException(status_code=403, detail="Authentication required for user management")
        return True
    
    def get_web_router(self) -> APIRouter:
        """Return FastAPI router with user management endpoints"""
        router = APIRouter(prefix=f"/plugins/{self.name}")
        
        # Import auth dependencies if auth is enabled
        if self.auth_enabled:
            try:
                from agent_system.auth.dependencies import require_admin
                # Create dependencies list for endpoints that require admin
                admin_deps = [Depends(require_admin)] if require_admin else []
            except ImportError:
                logger.warning("Auth dependencies not available, endpoints will not have auth protection")
                require_admin = None
                admin_deps = []
        else:
            require_admin = None
            admin_deps = []
        
        @router.get("/", response_class=HTMLResponse)
        async def user_management_home(request: Request):
            """User management dashboard"""
            if not self.auth_enabled:
                return self.templates.TemplateResponse(
                    "auth_disabled.html",
                    {"request": request, "plugin_name": self.name}
                )
            
            try:
                db = self._get_user_database()
                users = db.list_users(skip=0, limit=self.items_per_page)
                total_users = len(db.list_users(skip=0, limit=10000))  # Get all for count
                
                # Convert users to dicts with datetime serialization
                users_data = []
                for user in users:
                    user_dict = {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "full_name": user.full_name,
                        "is_active": user.is_active,
                        "role": user.role.value if hasattr(user.role, 'value') else str(user.role),
                        "created_at": user.created_at.isoformat() if user.created_at else None,
                        "last_login": user.last_login.isoformat() if user.last_login else None,
                        "has_api_key": bool(getattr(user, 'api_key', None))
                    }
                    users_data.append(user_dict)
                
                return self.templates.TemplateResponse(
                    "dashboard.html",
                    {
                        "request": request,
                        "plugin_name": self.name,
                        "users": users_data,
                        "total_users": total_users,
                        "items_per_page": self.items_per_page,
                        "show_api_keys": self.show_api_keys
                    }
                )
            except Exception as e:
                logger.error(f"Error loading user dashboard: {e}")
                return HTMLResponse(
                    content=f"<h1>Error</h1><p>Failed to load users: {str(e)}</p>",
                    status_code=500
                )
        
        @router.get("/users/list")
        async def list_users(skip: int = 0, limit: int = None):
            """API endpoint to list users"""
            self._check_admin_permission(None)
            
            try:
                db = self._get_user_database()
                actual_limit = limit if limit is not None else self.items_per_page
                users = db.list_users(skip=skip, limit=actual_limit)
                
                # Convert to dict and remove sensitive data
                users_data = []
                for user in users:
                    user_dict = {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "full_name": user.full_name,
                        "is_active": user.is_active,
                        "role": user.role,
                        "created_at": user.created_at,
                        "last_login": user.last_login,
                        "has_api_key": bool(getattr(user, 'api_key', None))
                    }
                    users_data.append(user_dict)
                
                return {"users": users_data, "skip": skip, "limit": actual_limit}
            except Exception as e:
                logger.error(f"Error listing users: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/users", dependencies=admin_deps)
        async def create_user(user_data: CreateUserRequest):
            """Create a new user (admin only)"""
            try:
                from agent_system.auth.database import UserDatabase
                from agent_system.auth.models import UserRole, UserCreate
                
                db = UserDatabase()
                
                # Validate role
                try:
                    user_role = UserRole(user_data.role.lower())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid role: {user_data.role}")
                
                # Check if username or email already exists
                existing_user = db.get_user_by_username(user_data.username)
                if existing_user:
                    raise HTTPException(status_code=400, detail="Username already exists")
                
                existing_email = db.get_user_by_email(user_data.email)
                if existing_email:
                    raise HTTPException(status_code=400, detail="Email already exists")
                
                # Create UserCreate object
                user_create = UserCreate(
                    username=user_data.username,
                    email=user_data.email,
                    password=user_data.password,  # Will be hashed by create_user
                    full_name=user_data.full_name,
                    role=user_role,
                    is_active=user_data.is_active
                )
                
                # Create user
                user = db.create_user(user_create)
                
                return {
                    "message": f"User '{user_data.username}' created successfully",
                    "username": user.username,
                    "user": {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "full_name": user.full_name,
                        "role": user.role,
                        "is_active": user.is_active
                    }
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error creating user: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/users/{user_id}")
        async def get_user(user_id: int):
            """API endpoint to get user details"""
            self._check_admin_permission(None)
            
            try:
                db = self._get_user_database()
                user = db.get_user_by_id(user_id)
                
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                user_dict = {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "is_active": user.is_active,
                    "role": user.role,
                    "created_at": user.created_at,
                    "updated_at": user.updated_at,
                    "last_login": user.last_login,
                    "has_api_key": bool(getattr(user, 'api_key', None))
                }
                
                return user_dict
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting user {user_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.put("/users/{user_id}", dependencies=admin_deps)
        async def update_user(user_id: int, user_data: UpdateUserRequest):
            """Update user details (admin only)"""
            try:
                from agent_system.auth.database import UserDatabase
                from agent_system.auth.models import UserRole, UserUpdate
                
                db = UserDatabase()
                
                # Get existing user
                user = db.get_user_by_id(user_id)
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Validate username uniqueness if provided
                if user_data.username is not None:
                    existing = db.get_user_by_username(user_data.username)
                    if existing and existing.id != user_id:
                        raise HTTPException(status_code=400, detail="Username already exists")
                
                # Validate email uniqueness if provided
                if user_data.email is not None:
                    existing = db.get_user_by_email(user_data.email)
                    if existing and existing.id != user_id:
                        raise HTTPException(status_code=400, detail="Email already exists")
                
                # Convert role string to UserRole if provided
                user_role = None
                if user_data.role is not None:
                    try:
                        user_role = UserRole(user_data.role.lower())
                    except ValueError:
                        raise HTTPException(status_code=400, detail=f"Invalid role: {user_data.role}")
                
                # Build UserUpdate object (username is not updateable in UserUpdate model)
                update_obj = UserUpdate(
                    email=user_data.email,
                    full_name=user_data.full_name,
                    role=user_role,
                    is_active=user_data.is_active
                )
                
                # Update user
                updated_user = db.update_user(user_id, update_obj)
                
                return {
                    "message": f"User '{updated_user.username}' updated successfully",
                    "user": {
                        "id": updated_user.id,
                        "username": updated_user.username,
                        "email": updated_user.email,
                        "full_name": updated_user.full_name,
                        "role": updated_user.role,
                        "is_active": updated_user.is_active
                    }
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error updating user {user_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/users/{user_id}/toggle-active")
        async def toggle_user_active(
            user_id: int,
            admin_user=Depends(require_admin) if require_admin else None
        ):
            """Toggle user active status (admin only)"""
            try:
                db = self._get_user_database()
                user = db.get_user_by_id(user_id)
                
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Toggle active status
                from agent_system.auth.models import UserUpdate
                updated_user = db.update_user(user_id, UserUpdate(is_active=not user.is_active))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "is_active": updated_user.is_active,
                    "message": f"User {'activated' if updated_user.is_active else 'deactivated'}"
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error toggling user {user_id} active status: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.post("/users/{user_id}/change-role")
        async def change_user_role(
            user_id: int, 
            role: str,
            admin_user=Depends(require_admin) if require_admin else None
        ):
            """Change user role (admin only)"""
            try:
                from agent_system.auth.models import UserRole, UserUpdate
                
                # Validate role
                try:
                    new_role = UserRole(role)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
                
                db = self._get_user_database()
                user = db.get_user_by_id(user_id)
                
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # Update role
                updated_user = db.update_user(user_id, UserUpdate(role=new_role))
                
                return {
                    "success": True,
                    "user_id": user_id,
                    "role": updated_user.role,
                    "message": f"User role changed to {new_role.value}"
                }
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error changing user {user_id} role: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.delete("/users/{user_id}")
        async def delete_user(
            user_id: int,
            admin_user=Depends(require_admin) if require_admin else None
        ):
            """Delete a user (admin only)"""
            try:
                db = self._get_user_database()
                user = db.get_user_by_id(user_id)
                
                if not user:
                    raise HTTPException(status_code=404, detail="User not found")
                
                # TODO: Add check for self-delete if allow_self_delete is False
                
                success = db.delete_user(user_id)
                
                if success:
                    return {"success": True, "user_id": user_id, "message": "User deleted"}
                else:
                    raise HTTPException(status_code=500, detail="Failed to delete user")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error deleting user {user_id}: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/stats")
        async def get_user_stats():
            """Get user statistics"""
            self._check_admin_permission(None)
            
            try:
                db = self._get_user_database()
                all_users = db.list_users(skip=0, limit=10000)
                
                stats = {
                    "total_users": len(all_users),
                    "active_users": sum(1 for u in all_users if u.is_active),
                    "inactive_users": sum(1 for u in all_users if not u.is_active),
                    "admins": sum(1 for u in all_users if u.role == "ADMIN"),
                    "regular_users": sum(1 for u in all_users if u.role == "USER"),
                    "guests": sum(1 for u in all_users if u.role == "GUEST"),
                    "users_with_api_keys": sum(1 for u in all_users if getattr(u, 'api_key', None))
                }
                
                return stats
            except Exception as e:
                logger.error(f"Error getting user stats: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        
        @router.get("/static/{file_path:path}")
        async def serve_static(file_path: str):
            """Serve static files (CSS, JS)"""
            from fastapi.responses import FileResponse
            
            static_dir = Path(__file__).parent / "static"
            file_full_path = static_dir / file_path
            
            # Security check - ensure file is within static directory
            try:
                file_full_path = file_full_path.resolve()
                static_dir = static_dir.resolve()
                if not str(file_full_path).startswith(str(static_dir)):
                    raise HTTPException(status_code=403, detail="Access denied")
                
                if not file_full_path.exists() or not file_full_path.is_file():
                    raise HTTPException(status_code=404, detail="File not found")
                
                # Determine content type
                content_type = "text/plain"
                if file_path.endswith(".js"):
                    content_type = "application/javascript"
                elif file_path.endswith(".css"):
                    content_type = "text/css"
                elif file_path.endswith(".html"):
                    content_type = "text/html"
                elif file_path.endswith(".json"):
                    content_type = "application/json"
                
                return FileResponse(
                    path=file_full_path,
                    media_type=content_type,
                    headers={"Cache-Control": "public, max-age=3600"}
                )
            except Exception as e:
                logger.error(f"Error serving static file {file_path}: {e}")
                raise HTTPException(status_code=500, detail="Internal server error")
        
        return router
    
    def get_static_assets(self) -> Dict[str, Path]:
        """Return static asset paths"""
        static_dir = Path(__file__).parent / "static"
        if not static_dir.exists():
            return {}
        
        assets = {}
        for file in static_dir.glob("*.js"):
            assets[file.name] = file
        for file in static_dir.glob("*.css"):
            assets[file.name] = file
        
        return assets
    
    def get_panels(self) -> List[Dict[str, Any]]:
        """Return panel definitions for main UI integration"""
        if not self.auth_enabled:
            return [{
                "id": "user_management_disabled",
                "title": "User Management (Disabled)",
                "description": "Enable authentication to use user management",
                "icon": "users-slash",
                "url": f"/plugins/{self.name}/",
                "category": "admin",
                "order": 100,
                "enabled": False
            }]
        
        return [
            {
                "id": "user_management",
                "title": "User Management",
                "description": "Manage users, roles, and permissions",
                "icon": "users",
                "url": f"/plugins/{self.name}/",
                "category": "admin",
                "order": 10,
                "requires_admin": True
            },
            {
                "id": "user_stats",
                "title": "User Statistics",
                "description": "View user activity and statistics",
                "icon": "chart-bar",
                "url": f"/plugins/{self.name}/stats",
                "category": "admin",
                "order": 11,
                "requires_admin": True
            }
        ]
    
    # Note: get_menu_items() removed - menu items are now defined in schema.yaml
    # under web_ui.menu.items section for cleaner configuration
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration for this plugin"""
        return {
            "requires_auth": True,
            "requires_admin": True,
            "rate_limit": "30/minute",
            "allowed_methods": ["GET", "POST", "DELETE"]
        }
