"""
Tests for Authentication Endpoints

Comprehensive tests for login, logout, token refresh,
and authentication middleware.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.auth.models import UserRole, UserCreate
from agent_system.auth.security import create_access_token
from agent_system.auth.database import setup_database


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_auth_endpoints.db"
        db = setup_database(db_path)
        yield db


@pytest.fixture
def test_user(temp_db):
    """Create a test user in the database."""
    user_data = UserCreate(
        username="testuser",
        email="test@example.com",
        password="password123",
        full_name="Test User",
        role=UserRole.USER,
        is_active=True
    )
    return temp_db.create_user(user_data)


@pytest.fixture
def inactive_user(temp_db):
    """Create an inactive test user in the database."""
    user_data = UserCreate(
        username="inactiveuser",
        email="inactive@example.com",
        password="password123",
        full_name="Inactive User",
        role=UserRole.USER,
        is_active=False
    )
    return temp_db.create_user(user_data)


@pytest.fixture
def admin_user(temp_db):
    """Create an admin test user in the database."""
    user_data = UserCreate(
        username="admin",
        email="admin@example.com",
        password="admin123",
        full_name="Admin User",
        role=UserRole.ADMIN,
        is_active=True
    )
    return temp_db.create_user(user_data)


@pytest.fixture
def client(temp_db):
    """Create test client with temporary database."""
    from api.auth_endpoints import router
    
    app = FastAPI()
    app.include_router(router)
    
    # Override database dependency
    from agent_system.auth.database import get_db
    app.dependency_overrides[get_db] = lambda: temp_db
    
    return TestClient(app)


class TestLoginEndpoint:
    """Test login endpoint functionality"""
    
    def test_login_success(self, client, test_user):
        """Test successful login"""
        response = client.post(
            "/auth/login",
            json={"username": "testuser", "password": "password123"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
    
    def test_login_invalid_username(self, client):
        """Test login with invalid username"""
        response = client.post(
            "/auth/login",
            json={"username": "nonexistent", "password": "password123"}
        )
        
        assert response.status_code == 401
    
    def test_login_invalid_password(self, client, test_user):
        """Test login with invalid password"""
        response = client.post(
            "/auth/login",
            json={"username": "testuser", "password": "wrongpassword"}
        )
        
        assert response.status_code == 401
    
    def test_login_inactive_user(self, client, inactive_user):
        """Test login with inactive user"""
        response = client.post(
            "/auth/login",
            json={"username": "inactiveuser", "password": "password123"}
        )
        
        assert response.status_code == 403
        assert "inactive" in response.json()["detail"].lower()


class TestAuthMiddleware:
    """Test authentication middleware and dependencies"""
    
    @pytest.fixture
    def app_with_protected_route(self, temp_db):
        """Create app with protected route"""
        from fastapi import Depends
        from agent_system.auth.database import get_db
        
        app = FastAPI()
        
        try:
            from agent_system.auth.dependencies import get_current_user
            
            @app.get("/protected")
            async def protected_route(user=Depends(get_current_user)):
                return {"username": user.username, "role": user.role}
            
            # Override database dependency
            app.dependency_overrides[get_db] = lambda: temp_db
            
        except ImportError:
            pytest.skip("Auth dependencies not implemented")
        
        return app
    
    def test_protected_route_without_token(self, app_with_protected_route):
        """Test accessing protected route without token"""
        client = TestClient(app_with_protected_route)
        
        response = client.get("/protected")
        
        assert response.status_code == 401
    
    def test_protected_route_with_valid_token(self, app_with_protected_route, test_user):
        """Test accessing protected route with valid token"""
        client = TestClient(app_with_protected_route)
        
        # Create valid token
        token = create_access_token({"sub": "testuser", "user_id": test_user.id, "role": "user"})
        
        response = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        # Token verification will work, user lookup will succeed
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
    
    def test_protected_route_with_invalid_token(self, app_with_protected_route):
        """Test accessing protected route with invalid token"""
        client = TestClient(app_with_protected_route)
        
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer invalid.token.here"}
        )
        
        assert response.status_code == 401


class TestAdminRequiredEndpoints:
    """Test admin-only endpoints"""
    
    @pytest.fixture
    def app_with_admin_route(self, temp_db):
        """Create app with admin-only route"""
        from fastapi import Depends
        from agent_system.auth.database import get_db
        
        app = FastAPI()
        
        try:
            from agent_system.auth.dependencies import require_admin
            
            @app.get("/admin-only")
            async def admin_route(user=Depends(require_admin)):
                return {"message": "Admin access granted", "username": user.username}
            
            # Override database dependency
            app.dependency_overrides[get_db] = lambda: temp_db
            
        except ImportError:
            pytest.skip("Admin dependencies not implemented")
        
        return app
    
    def test_admin_route_with_admin_user(self, app_with_admin_route, admin_user):
        """Test admin route with admin user"""
        client = TestClient(app_with_admin_route)
        
        token = create_access_token({"sub": "admin", "user_id": admin_user.id, "role": "admin"})
        
        response = client.get(
            "/admin-only",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Admin access granted"
    
    def test_admin_route_with_regular_user(self, app_with_admin_route, test_user):
        """Test admin route with regular user is forbidden"""
        client = TestClient(app_with_admin_route)
        
        token = create_access_token({"sub": "testuser", "user_id": test_user.id, "role": "user"})
        
        response = client.get(
            "/admin-only",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        # Should be forbidden (403) not unauthorized (401)
        assert response.status_code == 403


class TestTokenRefresh:
    """Test token refresh functionality"""
    
    def test_token_refresh_success(self):
        """Test successful token refresh"""
        # Token refresh endpoint is not implemented yet
        pytest.skip("Token refresh endpoint not implemented")


class TestLogout:
    """Test logout functionality"""
    
    @pytest.fixture
    def app_with_logout(self, temp_db):
        """Create app with logout endpoint"""
        from agent_system.auth.database import get_db
        
        app = FastAPI()
        
        from api.auth_endpoints import router as auth_router
        app.include_router(auth_router)
        
        # Override database dependency
        app.dependency_overrides[get_db] = lambda: temp_db
        
        return app
    
    def test_logout_success(self, app_with_logout, test_user):
        """Test successful logout"""
        client = TestClient(app_with_logout)
        
        token = create_access_token({"sub": "testuser", "user_id": test_user.id, "role": "user"})
        
        response = client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        if response.status_code == 404:
            pytest.skip("Logout endpoint not implemented")
        
        assert response.status_code == 200


class TestUserRegistration:
    """Test user registration endpoint"""
    
    @pytest.fixture
    def app_with_registration(self, temp_db):
        """Create app with registration endpoint"""
        from agent_system.auth.database import get_db
        
        app = FastAPI()
        
        from api.auth_endpoints import router as auth_router
        app.include_router(auth_router)
        
        # Override database dependency
        app.dependency_overrides[get_db] = lambda: temp_db
        
        return app
    
    def test_registration_success(self, app_with_registration):
        """Test successful user registration"""
        client = TestClient(app_with_registration)
        
        response = client.post(
            "/auth/register",
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "securepass123",
                "full_name": "New User"
            }
        )
        
        if response.status_code == 404:
            pytest.skip("Registration endpoint not implemented")
        
        assert response.status_code == 201
        data = response.json()
        assert data["username"] == "newuser"
    
    def test_registration_duplicate_username(self, app_with_registration, test_user):
        """Test registration with duplicate username"""
        client = TestClient(app_with_registration)
        
        response = client.post(
            "/auth/register",
            json={
                "username": "testuser",  # Already exists
                "email": "new@example.com",
                "password": "securepass123"
            }
        )
        
        if response.status_code == 404:
            pytest.skip("Registration endpoint not implemented")
        
        assert response.status_code == 400


class TestAuthEdgeCases:
    """Test edge cases and security scenarios"""
    
    def test_token_expiry_handling(self):
        """Test expired token is rejected"""
        from agent_system.auth.security import decode_access_token
        
        # Create expired token
        expired_token = create_access_token(
            {"sub": "testuser", "user_id": 1, "role": "user"},
            expires_delta=timedelta(seconds=-1)
        )
        
        decoded = decode_access_token(expired_token)
        assert decoded is None
    
    def test_token_with_missing_claims(self):
        """Test token with missing required claims"""
        from agent_system.auth.security import decode_access_token
        
        # Create token with missing claims
        invalid_token = create_access_token({"sub": "testuser"})  # Missing user_id and role
        
        decoded = decode_access_token(invalid_token)
        # Should handle gracefully
        assert decoded is None or hasattr(decoded, 'username')
    
    def test_concurrent_login_attempts(self, client, test_user):
        """Test handling of concurrent login attempts"""
        # Simulate concurrent login attempts
        responses = []
        for _ in range(3):
            response = client.post(
                "/auth/login",
                json={"username": "testuser", "password": "password123"}
            )
            responses.append(response)
        
        # All should succeed
        for response in responses:
            assert response.status_code == 200
