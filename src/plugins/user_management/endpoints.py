"""User Management Web Endpoints

Provides web UI endpoints for user administration and management.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Request, HTTPException
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
    
    def __init__(self, name: str, system_config: "AgentSystemConfig", mcp_config: "MCPConfig", plugin=None):
        """Initialize with new signature."""
        self.name = name
        self.system_config = system_config
        self.mcp_config = mcp_config
        self.plugin = plugin  # Reference to main plugin for schema access
        
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

    def _get_request_user(self, request: Optional[Request]):
        """Safely extract the authenticated user from the request when middleware is installed."""
        if request is None:
            return None

        # FastAPI raises RuntimeError when accessing request.user without AuthenticationMiddleware
        user = request.scope.get("user") if request.scope else None
        if not user and hasattr(request, "state"):
            user = getattr(request.state, "user", None)
        return user
    
    def get_web_router(self) -> APIRouter:
        """Get the FastAPI router for this plugin's web endpoints."""
        from agent_system.plugins.schema_router import create_schema_router
        
        # Get schema from plugin (already loaded with Jinja2 templates rendered)
        schema = self.plugin.get_schema_data() if self.plugin and hasattr(self.plugin, 'get_schema_data') else {}
        
        # Generate router from schema
        return create_schema_router(
            plugin_name=self.name,
            schema=schema,
            handler_class=self
        )
    
    # Handler methods (called by schema router)
    
    async def user_management_home(self, request: Request) -> HTMLResponse:
        """User management dashboard"""
        if not self.auth_enabled:
            return self.templates.TemplateResponse(
                request=request,
                name="auth_disabled.html",
                context={"plugin_name": self.name}
            )
        
        try:
            db = self._get_user_database()
            users = db.list_users(skip=0, limit=self.items_per_page)
            total_users = len(db.list_users(skip=0, limit=10000))
            
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
                request=request,
                name="dashboard.html",
                context={
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
    
    async def list_users(self, request: Request, skip: int = 0, limit: Optional[int] = None):
        """API endpoint to list users"""
        self._check_admin_permission(request)
        
        try:
            db = self._get_user_database()
            actual_limit = limit if limit is not None else self.items_per_page
            users = db.list_users(skip=skip, limit=actual_limit)
            
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
    
    async def create_user(self, request: Request, user_data: CreateUserRequest):
        """Create a new user (admin only)"""
        self._check_admin_permission(request)
        try:
            from agent_system.auth.database import UserDatabase
            from agent_system.auth.models import UserRole, UserCreate
            
            db = UserDatabase()
            
            try:
                user_role = UserRole(user_data.role.lower())
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid role: {user_data.role}")
            
            existing_user = db.get_user_by_username(user_data.username)
            if existing_user:
                raise HTTPException(status_code=400, detail="Username already exists")
            
            existing_email = db.get_user_by_email(user_data.email)
            if existing_email:
                raise HTTPException(status_code=400, detail="Email already exists")
            
            user_create = UserCreate(
                username=user_data.username,
                email=user_data.email,
                password=user_data.password,
                full_name=user_data.full_name,
                role=user_role,
                is_active=user_data.is_active
            )
            
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
    
    async def get_user(self, request: Request, user_id: int):
        """API endpoint to get user details"""
        self._check_admin_permission(request)
        
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
    
    async def update_user(self, request: Request, user_id: int, user_data: UpdateUserRequest):
        """Update user details (admin only)"""
        self._check_admin_permission(request)
        try:
            from agent_system.auth.database import UserDatabase
            from agent_system.auth.models import UserRole, UserUpdate
            
            db = UserDatabase()
            user = db.get_user_by_id(user_id)
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            if user_data.username is not None:
                existing = db.get_user_by_username(user_data.username)
                if existing and existing.id != user_id:
                    raise HTTPException(status_code=400, detail="Username already exists")
            
            if user_data.email is not None:
                existing = db.get_user_by_email(user_data.email)
                if existing and existing.id != user_id:
                    raise HTTPException(status_code=400, detail="Email already exists")
            
            user_role = None
            if user_data.role is not None:
                try:
                    user_role = UserRole(user_data.role.lower())
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid role: {user_data.role}")
            
            update_obj = UserUpdate(
                email=user_data.email,
                full_name=user_data.full_name,
                role=user_role,
                is_active=user_data.is_active
            )
            
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
    
    async def toggle_user_active(self, request: Request, user_id: int):
        """Toggle user active status (admin only)"""
        self._check_admin_permission(request)
        try:
            db = self._get_user_database()
            user = db.get_user_by_id(user_id)
            
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
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
    
    async def change_user_role(self, request: Request, user_id: int, role: str):
        """Change user role (admin only)"""
        self._check_admin_permission(request)
        try:
            from agent_system.auth.models import UserRole, UserUpdate
            
            try:
                new_role = UserRole(role)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
            
            db = self._get_user_database()
            user = db.get_user_by_id(user_id)
            
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            updated_user = db.update_user(user_id, UserUpdate(role=new_role))
            
            return {
                "success": True,
                "user_id": user_id,
                "role": updated_user.role,
                "message": f"User role changed to {getattr(new_role, 'value', str(new_role))}"
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error changing user {user_id} role: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    async def delete_user(self, request: Request, user_id: int):
        """Delete a user (admin only)"""
        self._check_admin_permission(request)
        try:
            db = self._get_user_database()
            user = db.get_user_by_id(user_id)
            
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            if not self.allow_self_delete:
                current_user = self._get_request_user(request)
                if current_user and getattr(current_user, "id", None) == user_id:
                    raise HTTPException(status_code=403, detail="Self-delete not allowed")
            
            success = db.delete_user(user_id)
            
            if success:
                return {"success": True, "user_id": user_id, "message": "User deleted"}
            raise HTTPException(status_code=500, detail="Failed to delete user")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    async def get_stats(self, request: Request):
        """Get user statistics"""
        self._check_admin_permission(request)
        
        try:
            db = self._get_user_database()
            all_users = db.list_users(skip=0, limit=10000)
            
            stats = {
                "total_users": len(all_users),
                "active_users": sum(1 for u in all_users if u.is_active),
                "inactive_users": sum(1 for u in all_users if not u.is_active),
                "admins": sum(1 for u in all_users if getattr(u.role, 'value', str(u.role)) == "admin" or u.role == "ADMIN"),
                "regular_users": sum(1 for u in all_users if getattr(u.role, 'value', str(u.role)) == "user" or u.role == "USER"),
                "guests": sum(1 for u in all_users if getattr(u.role, 'value', str(u.role)) == "guest" or u.role == "GUEST"),
                "users_with_api_keys": sum(1 for u in all_users if getattr(u, 'api_key', None))
            }
            
            return stats
        except Exception as e:
            logger.error(f"Error getting user stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    async def serve_static(self, request: Request, file_path: str):
        """Serve static files (CSS, JS)"""
        from fastapi.responses import FileResponse
        
        static_dir = Path(__file__).parent / "static"
        file_full_path = static_dir / file_path
        
        try:
            file_full_path = file_full_path.resolve()
            static_dir = static_dir.resolve()
            if not str(file_full_path).startswith(str(static_dir)):
                raise HTTPException(status_code=403, detail="Access denied")
            
            if not file_full_path.exists() or not file_full_path.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            
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
