"""
Tests for Multi-User Authentication System

Comprehensive tests for user management, authentication, and authorization.
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
import tempfile

from agent_system.auth.models import (
    UserCreate,
    UserUpdate,
    UserRole,
)
from agent_system.auth.database import setup_database
from agent_system.auth.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_users.db"
        db = setup_database(db_path)
        yield db


@pytest.fixture
def sample_user(temp_db):
    """Create a sample user for testing."""
    user_data = UserCreate(
        username="testuser",
        email="test@example.com",
        password="testpass123",
        full_name="Test User",
        role=UserRole.USER,
        is_active=True
    )
    return temp_db.create_user(user_data)


class TestPasswordHashing:
    """Test password hashing and verification."""
    
    def test_password_hashing(self):
        """Test password can be hashed and verified."""
        password = "securepassword123"
        hashed = get_password_hash(password)
        
        assert hashed != password
        assert verify_password(password, hashed)
        assert not verify_password("wrongpassword", hashed)
    
    def test_different_hashes(self):
        """Test same password produces different hashes."""
        password = "samepassword"
        hash1 = get_password_hash(password)
        hash2 = get_password_hash(password)
        
        assert hash1 != hash2
        assert verify_password(password, hash1)
        assert verify_password(password, hash2)


class TestJWTTokens:
    """Test JWT token generation and validation."""
    
    def test_create_and_decode_token(self):
        """Test token creation and decoding."""
        data = {"sub": "testuser", "user_id": 1, "role": "user"}
        token = create_access_token(data)
        
        assert token is not None
        assert isinstance(token, str)
        
        decoded = decode_access_token(token)
        assert decoded is not None
        assert decoded.username == "testuser"
        assert decoded.user_id == 1
        assert decoded.role == UserRole.USER
    
    def test_expired_token(self):
        """Test expired token is rejected."""
        data = {"sub": "testuser", "user_id": 1, "role": "user"}
        token = create_access_token(data, expires_delta=timedelta(seconds=-1))
        
        decoded = decode_access_token(token)
        assert decoded is None
    
    def test_invalid_token(self):
        """Test invalid token is rejected."""
        decoded = decode_access_token("invalid.token.here")
        assert decoded is None


class TestAPIKeys:
    """Test API key generation and validation."""
    
    def test_generate_api_key(self):
        """Test API key generation."""
        key = generate_api_key()
        
        assert key is not None
        assert isinstance(key, str)
        assert len(key) > 20
    
    def test_hash_and_verify_api_key(self):
        """Test API key hashing and verification."""
        key = generate_api_key()
        hashed = hash_api_key(key)
        
        assert hashed != key
        assert verify_api_key(key, hashed)
        assert not verify_api_key("wrongkey", hashed)


class TestUserDatabase:
    """Test user database operations."""
    
    def test_create_user(self, temp_db):
        """Test user creation."""
        user_data = UserCreate(
            username="newuser",
            email="new@example.com",
            password="password123",
            full_name="New User",
            role=UserRole.USER,
            is_active=True
        )
        
        user = temp_db.create_user(user_data)
        
        assert user.id is not None
        assert user.username == "newuser"
        assert user.email == "new@example.com"
        assert user.full_name == "New User"
        assert user.role == UserRole.USER
        assert user.is_active is True
        assert user.created_at is not None
    
    def test_duplicate_username(self, temp_db, sample_user):
        """Test duplicate username is rejected."""
        user_data = UserCreate(
            username=sample_user.username,
            email="different@example.com",
            password="password123",
        )
        
        with pytest.raises(ValueError, match="already exists"):
            temp_db.create_user(user_data)
    
    def test_duplicate_email(self, temp_db, sample_user):
        """Test duplicate email is rejected."""
        user_data = UserCreate(
            username="differentuser",
            email=sample_user.email,
            password="password123",
        )
        
        with pytest.raises(ValueError, match="already exists"):
            temp_db.create_user(user_data)
    
    def test_get_user_by_id(self, temp_db, sample_user):
        """Test getting user by ID."""
        user = temp_db.get_user_by_id(sample_user.id)
        
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == sample_user.username
    
    def test_get_user_by_username(self, temp_db, sample_user):
        """Test getting user by username."""
        user = temp_db.get_user_by_username(sample_user.username)
        
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == sample_user.username
    
    def test_get_user_by_email(self, temp_db, sample_user):
        """Test getting user by email."""
        user = temp_db.get_user_by_email(sample_user.email)
        
        assert user is not None
        assert user.id == sample_user.id
        assert user.email == sample_user.email
    
    def test_list_users(self, temp_db):
        """Test listing users."""
        # Create multiple users
        for i in range(5):
            user_data = UserCreate(
                username=f"user{i}",
                email=f"user{i}@example.com",
                password="password123",
            )
            temp_db.create_user(user_data)
        
        users = temp_db.list_users(limit=10)
        assert len(users) == 5
        
        users_limited = temp_db.list_users(limit=3)
        assert len(users_limited) == 3
        
        users_skipped = temp_db.list_users(skip=2, limit=10)
        assert len(users_skipped) == 3
    
    def test_update_user(self, temp_db, sample_user):
        """Test updating user."""
        update_data = UserUpdate(
            email="newemail@example.com",
            full_name="Updated Name",
            role=UserRole.ADMIN
        )
        
        updated_user = temp_db.update_user(sample_user.id, update_data)
        
        assert updated_user is not None
        assert updated_user.email == "newemail@example.com"
        assert updated_user.full_name == "Updated Name"
        assert updated_user.role == UserRole.ADMIN
        assert updated_user.updated_at is not None
    
    def test_update_password(self, temp_db, sample_user):
        """Test updating user password."""
        new_password = "newpassword456"
        update_data = UserUpdate(password=new_password)
        
        updated_user = temp_db.update_user(sample_user.id, update_data)
        
        assert updated_user is not None
        assert verify_password(new_password, updated_user.hashed_password)
    
    def test_delete_user(self, temp_db, sample_user):
        """Test deleting user."""
        result = temp_db.delete_user(sample_user.id)
        assert result is True
        
        user = temp_db.get_user_by_id(sample_user.id)
        assert user is None
    
    def test_delete_nonexistent_user(self, temp_db):
        """Test deleting nonexistent user returns False."""
        result = temp_db.delete_user(99999)
        assert result is False
    
    def test_update_last_login(self, temp_db, sample_user):
        """Test updating last login timestamp."""
        temp_db.update_last_login(sample_user.id)
        
        user = temp_db.get_user_by_id(sample_user.id)
        assert user.last_login is not None
        assert isinstance(user.last_login, datetime)
    
    def test_generate_api_key(self, temp_db, sample_user):
        """Test generating API key for user."""
        api_key = temp_db.generate_user_api_key(sample_user.id)
        
        assert api_key is not None
        assert isinstance(api_key, str)
        assert len(api_key) > 20
        
        # Verify key is stored as hash
        user = temp_db.get_user_by_id(sample_user.id)
        assert user.api_key is not None
        assert user.api_key != api_key  # Should be hashed
        assert verify_api_key(api_key, user.api_key)
    
    def test_revoke_api_key(self, temp_db, sample_user):
        """Test revoking API key."""
        # Generate key first
        api_key = temp_db.generate_user_api_key(sample_user.id)
        assert api_key is not None
        
        # Revoke key
        result = temp_db.revoke_user_api_key(sample_user.id)
        assert result is True
        
        # Verify key is revoked
        user = temp_db.get_user_by_id(sample_user.id)
        assert user.api_key is None
    
    def test_get_user_by_api_key(self, temp_db, sample_user):
        """Test getting user by API key."""
        api_key = temp_db.generate_user_api_key(sample_user.id)
        api_key_hash = hash_api_key(api_key)
        
        user = temp_db.get_user_by_api_key(api_key_hash)
        
        assert user is not None
        assert user.id == sample_user.id
        assert user.username == sample_user.username


class TestUserRoles:
    """Test user role functionality."""
    
    def test_admin_role(self, temp_db):
        """Test creating admin user."""
        user_data = UserCreate(
            username="admin",
            email="admin@example.com",
            password="adminpass",
            role=UserRole.ADMIN
        )
        
        user = temp_db.create_user(user_data)
        assert user.role == UserRole.ADMIN
    
    def test_guest_role(self, temp_db):
        """Test creating guest user."""
        user_data = UserCreate(
            username="guest",
            email="guest@example.com",
            password="guestpass",
            role=UserRole.GUEST
        )
        
        user = temp_db.create_user(user_data)
        assert user.role == UserRole.GUEST
    
    def test_role_promotion(self, temp_db, sample_user):
        """Test promoting user to admin."""
        update_data = UserUpdate(role=UserRole.ADMIN)
        updated_user = temp_db.update_user(sample_user.id, update_data)
        
        assert updated_user.role == UserRole.ADMIN
    
    def test_role_demotion(self, temp_db):
        """Test demoting admin to user."""
        # Create admin
        user_data = UserCreate(
            username="admin",
            email="admin@example.com",
            password="adminpass",
            role=UserRole.ADMIN
        )
        admin_user = temp_db.create_user(user_data)
        
        # Demote to user
        update_data = UserUpdate(role=UserRole.USER)
        updated_user = temp_db.update_user(admin_user.id, update_data)
        
        assert updated_user.role == UserRole.USER


class TestUserActivation:
    """Test user activation/deactivation."""
    
    def test_deactivate_user(self, temp_db, sample_user):
        """Test deactivating user."""
        update_data = UserUpdate(is_active=False)
        updated_user = temp_db.update_user(sample_user.id, update_data)
        
        assert updated_user.is_active is False
    
    def test_activate_user(self, temp_db):
        """Test activating inactive user."""
        # Create inactive user
        user_data = UserCreate(
            username="inactive",
            email="inactive@example.com",
            password="password123",
            is_active=False
        )
        inactive_user = temp_db.create_user(user_data)
        
        # Activate user
        update_data = UserUpdate(is_active=True)
        updated_user = temp_db.update_user(inactive_user.id, update_data)
        
        assert updated_user.is_active is True
    
    def test_create_inactive_user(self, temp_db):
        """Test creating user as inactive."""
        user_data = UserCreate(
            username="inactive",
            email="inactive@example.com",
            password="password123",
            is_active=False
        )
        
        user = temp_db.create_user(user_data)
        assert user.is_active is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
