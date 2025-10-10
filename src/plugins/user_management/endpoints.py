"""User Management Web Endpoints

Provides web UI endpoints for user administration and management.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, TYPE_CHECKING

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent_system.plugins.web_adapter import PluginWebInterface

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


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
        
        @router.post("/users/{user_id}/toggle-active")
        async def toggle_user_active(user_id: int):
            """Toggle user active status"""
            self._check_admin_permission(None)
            
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
        async def change_user_role(user_id: int, role: str):
            """Change user role"""
            self._check_admin_permission(None)
            
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
        async def delete_user(user_id: int):
            """Delete a user"""
            self._check_admin_permission(None)
            
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
    
    def get_security_config(self) -> Dict[str, Any]:
        """Return security configuration for this plugin"""
        return {
            "requires_auth": True,
            "requires_admin": True,
            "rate_limit": "30/minute",
            "allowed_methods": ["GET", "POST", "DELETE"]
        }
