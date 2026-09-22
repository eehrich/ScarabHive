"""
Database Layer for User Management

Provides SQLite-based user storage with support for PostgreSQL via SQLAlchemy.
Includes CRUD operations for user management.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List
from contextlib import contextmanager
import logging

from pydantic import ValidationError

from agent_system.auth.models import UserInDB, UserCreate, UserUpdate, UserRole, UserPreferences
from agent_system.auth.security import get_password_hash, generate_api_key, hash_api_key


logger = logging.getLogger(__name__)

# Default database path (override via config)
DEFAULT_DB_PATH = Path("data/users.db")


class UserDatabase:
    """User database manager using SQLite."""
    
    def __init__(self, db_path: Optional[Path] = None):
        """
        Initialize the user database.
        
        Args:
            db_path: Path to SQLite database file (default: data/users.db)
        """
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    full_name TEXT,
                    hashed_password TEXT NOT NULL,
                    api_key TEXT UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    last_login TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_username ON users(username)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_email ON users(email)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_key ON users(api_key)
            """)
            # A table of its own rather than a column of `users`: an existing database
            # gets it here without a migration, and the account's own columns -- read
            # and rebuilt field by field in many places -- stay as they are.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            logger.info(f"User database initialized at {self.db_path}")
    
    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    #: Identities that run without an account: agent-cli and agent-run default to
    #: "cli_user" (--session-user), unauthenticated web access to "anonymous". An
    #: account under either name would share their session directory and pass
    #: every check made by name -- and registration is open to anyone.
    RESERVED_USERNAMES = frozenset({"cli_user", "anonymous"})

    def create_user(self, user: UserCreate) -> UserInDB:
        """
        Create a new user.
        
        Args:
            user: User creation data
        
        Returns:
            Created user with ID
        
        Raises:
            ValueError: If username or email already exists
        """
        if user.username.casefold() in self.RESERVED_USERNAMES:
            raise ValueError(f"Username '{user.username}' is reserved")
        # Case does not make a name another one: a user's sessions live in a
        # directory named after it, and on Windows "Admin" and "admin" are one.
        with self._get_connection() as conn:
            taken = conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE",
                                 (user.username,)).fetchone()
        if taken:
            raise ValueError(f"Username '{user.username}' already exists")
        if self.get_user_by_email(user.email):
            raise ValueError(f"Email '{user.email}' already exists")
        
        hashed_password = get_password_hash(user.password)
        created_at = datetime.now(timezone.utc).isoformat()
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (
                    username, email, full_name, hashed_password,
                    is_active, role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                user.username,
                user.email,
                user.full_name,
                hashed_password,
                user.is_active,
                user.role.value,
                created_at
            ))
            conn.commit()
            user_id = cursor.lastrowid
        
        logger.info(f"Created user: {user.username} (ID: {user_id})")
        return self.get_user_by_id(user_id)  # type: ignore
    
    def get_user_by_id(self, user_id: int) -> Optional[UserInDB]:
        """Get user by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return self._row_to_user(row) if row else None
    
    def get_user_by_username(self, username: str) -> Optional[UserInDB]:
        """Get user by username."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
            row = cursor.fetchone()
            return self._row_to_user(row) if row else None
    
    def get_user_by_email(self, email: str) -> Optional[UserInDB]:
        """Get user by email."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
            row = cursor.fetchone()
            return self._row_to_user(row) if row else None
    
    def get_user_by_api_key(self, api_key_hash: str) -> Optional[UserInDB]:
        """Get user by hashed API key."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE api_key = ?", (api_key_hash,))
            row = cursor.fetchone()
            return self._row_to_user(row) if row else None
    
    def list_users(self, skip: int = 0, limit: int = 100) -> List[UserInDB]:
        """List all users with pagination."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, skip)
            )
            rows = cursor.fetchall()
            return [self._row_to_user(row) for row in rows]
    
    def count_users(self) -> int:
        """Total number of users (for pagination metadata)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return int(cursor.fetchone()[0])

    def update_user(self, user_id: int, update: UserUpdate) -> Optional[UserInDB]:
        """
        Update user information.
        
        Args:
            user_id: User ID to update
            update: Update data (only non-None fields are updated)
        
        Returns:
            Updated user or None if not found
        """
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        
        updates = {}
        if update.email is not None:
            # Same check as create_user: the UNIQUE constraint would otherwise
            # surface as an IntegrityError -- a 500 instead of a clear 400.
            owner = self.get_user_by_email(update.email)
            if owner and owner.id != user_id:
                raise ValueError(f"Email '{update.email}' already exists")
            updates["email"] = update.email
        if update.full_name is not None:
            updates["full_name"] = update.full_name
        if update.is_active is not None:
            updates["is_active"] = update.is_active
        if update.role is not None:
            updates["role"] = update.role.value
        if update.password is not None:
            updates["hashed_password"] = get_password_hash(update.password)
        
        if not updates:
            return user
        
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        # Build UPDATE query
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [user_id]
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE users SET {set_clause} WHERE id = ?",
                values
            )
            conn.commit()
        
        logger.info(f"Updated user ID {user_id}")
        return self.get_user_by_id(user_id)
    
    def delete_user(self, user_id: int) -> bool:
        """
        Delete a user.
        
        Args:
            user_id: User ID to delete
        
        Returns:
            True if deleted, False if not found
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted = cursor.rowcount > 0  # read before the next statement resets it
            cursor.execute("DELETE FROM user_preferences WHERE user_id = ?", (user_id,))
            conn.commit()
        
        if deleted:
            logger.info(f"Deleted user ID {user_id}")
        return deleted
    
    def get_preferences(self, user_id: int) -> UserPreferences:
        """A user's preferences, every key filled in; the defaults where they chose nothing."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT data FROM user_preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return UserPreferences()
        try:
            return UserPreferences.model_validate_json(row["data"])
        except ValidationError as e:
            # Only a stored choice this code no longer knows gets here -- a value
            # renamed since it was saved. Showing things the default way is all that
            # is lost; refusing would take the whole chat's display with it.
            logger.warning("Stored preferences of user ID %s no longer fit, using the defaults: %s",
                           user_id, e)
            return UserPreferences()

    def set_preferences(self, user_id: int, preferences: UserPreferences) -> UserPreferences:
        """Replace a user's preferences with `preferences`."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_preferences (user_id, data, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (user_id, preferences.model_dump_json(), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        return preferences

    def update_last_login(self, user_id: int) -> None:
        """Update user's last login timestamp."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id)
            )
            conn.commit()
    
    def generate_user_api_key(self, user_id: int) -> Optional[str]:
        """
        Generate and store a new API key for a user.
        
        Args:
            user_id: User ID
        
        Returns:
            Plain API key (save this, it won't be retrievable later)
        """
        user = self.get_user_by_id(user_id)
        if not user:
            return None
        
        api_key = generate_api_key()
        api_key_hash = hash_api_key(api_key)
        
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET api_key = ?, updated_at = ? WHERE id = ?",
                (api_key_hash, datetime.now(timezone.utc).isoformat(), user_id)
            )
            conn.commit()
        
        logger.info(f"Generated API key for user ID {user_id}")
        return api_key
    
    def revoke_user_api_key(self, user_id: int) -> bool:
        """Revoke a user's API key."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET api_key = NULL, updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), user_id)
            )
            conn.commit()
            revoked = cursor.rowcount > 0
        
        if revoked:
            logger.info(f"Revoked API key for user ID {user_id}")
        return revoked
    
    def _row_to_user(self, row: sqlite3.Row) -> UserInDB:
        """Convert database row to UserInDB model."""
        return UserInDB(
            id=row["id"],
            username=row["username"],
            email=row["email"],
            full_name=row["full_name"],
            hashed_password=row["hashed_password"],
            api_key=row["api_key"],
            is_active=bool(row["is_active"]),
            role=UserRole(row["role"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            last_login=datetime.fromisoformat(row["last_login"]) if row["last_login"] else None,
        )


# Global database instance (initialized in setup_auth)
_db: Optional[UserDatabase] = None


def get_db() -> UserDatabase:
    """Get the global database instance."""
    global _db
    if _db is None:
        _db = UserDatabase()
    return _db


def setup_database(db_path: Optional[Path] = None) -> UserDatabase:
    """
    Initialize the global database instance.
    
    Args:
        db_path: Optional custom database path
    
    Returns:
        Database instance
    """
    global _db
    _db = UserDatabase(db_path)
    return _db


# Convenience functions
def get_user_by_username(username: str) -> Optional[UserInDB]:
    """Get user by username."""
    return get_db().get_user_by_username(username)


def get_user_by_email(email: str) -> Optional[UserInDB]:
    """Get user by email."""
    return get_db().get_user_by_email(email)


def get_user_by_id(user_id: int) -> Optional[UserInDB]:
    """Get user by ID."""
    return get_db().get_user_by_id(user_id)
