"""
Test for PATCH /auth/me endpoint
"""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
import tempfile
from pathlib import Path

from api.auth_endpoints import router
from agent_system.auth.database import setup_database, get_db
from agent_system.auth.models import UserCreate, UserRole


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_users.db"
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
def client(temp_db):
    """Create test client with temporary database."""
    app = FastAPI()
    app.include_router(router)
    
    # Override database dependency
    app.dependency_overrides[get_db] = lambda: temp_db
    
    return TestClient(app)


def test_patch_me_update_email(client, test_user):
    """Test updating user email via PATCH /auth/me"""
    # Login first to get token
    login_response = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "password123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]
    
    # Update email
    response = client.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "newemail@example.com"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "newemail@example.com"
    assert data["username"] == "testuser"
    

def test_patch_me_update_full_name(client, test_user):
    """Test updating user full name via PATCH /auth/me"""
    # Login first
    login_response = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "password123"}
    )
    token = login_response.json()["access_token"]
    
    # Update full name
    response = client.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_name": "New Full Name"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["full_name"] == "New Full Name"


def test_patch_me_update_password(client, test_user):
    """Test updating password via PATCH /auth/me"""
    # Login with old password
    login_response = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "password123"}
    )
    token = login_response.json()["access_token"]
    
    # Update password
    response = client.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"password": "newpassword123"}
    )
    
    assert response.status_code == 200
    
    # Login with new password should work
    new_login = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "newpassword123"}
    )
    assert new_login.status_code == 200
    
    # Old password should not work
    old_login = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "password123"}
    )
    assert old_login.status_code == 401


def test_patch_me_unauthorized(client):
    """Test PATCH /auth/me without authentication"""
    response = client.patch(
        "/auth/me",
        json={"email": "hack@example.com"}
    )
    
    assert response.status_code == 401


def test_patch_me_multiple_fields(client, test_user):
    """Test updating multiple fields at once"""
    # Login
    login_response = client.post(
        "/auth/login",
        json={"username": "testuser", "password": "password123"}
    )
    token = login_response.json()["access_token"]
    
    # Update multiple fields
    response = client.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "email": "updated@example.com",
            "full_name": "Updated Name"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "updated@example.com"
    assert data["full_name"] == "Updated Name"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
