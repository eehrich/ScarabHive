"""
User Management CLI Commands

Provides command-line interface for user management operations.
"""

from __future__ import annotations

import getpass
import sys
import os
from pathlib import Path
from typing import Optional
import logging

import typer
from tabulate import tabulate

from agent_system.auth.database import setup_database, UserDatabase
from agent_system.auth.models import UserCreate, UserUpdate, UserRole
from agent_system.config.settings import load_settings


app = typer.Typer(help="User management commands")
logger = logging.getLogger(__name__)


def _supports_color() -> bool:
    """Check if terminal supports ANSI color codes.
    
    Respects NO_COLOR environment variable and checks if stdout is a TTY.
    """
    # Respect NO_COLOR standard
    if os.environ.get("NO_COLOR"):
        return False
    
    # Check if FORCE_COLOR is set (for CI/testing)
    if os.environ.get("FORCE_COLOR"):
        return True
    
    # Check if stdout is a TTY
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _colorize(text: str, color_code: str) -> str:
    """Wrap text in ANSI color codes when supported."""
    if not _supports_color():
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"


def get_configured_db() -> UserDatabase:
    """Get database instance from configuration."""
    try:
        config = load_settings()
        if config.auth and config.auth.database_path:
            db_path = Path(config.auth.database_path)
        else:
            db_path = Path("data/users.db")
        
        return setup_database(db_path)
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error loading configuration: {e}", err=True)
        raise typer.Exit(1)


@app.command("list")
def list_users(
    limit: int = typer.Option(100, help="Maximum number of users to show"),
    skip: int = typer.Option(0, help="Number of users to skip"),
):
    """List all users."""
    try:
        db = get_configured_db()
        users = db.list_users(skip=skip, limit=limit)
        
        if not users:
            typer.echo("No users found.")
            return
        
        # Prepare colorful table data
        headers = ["ID", "USERNAME", "EMAIL", "FULL NAME", "ROLE", "ACTIVE", "CREATED"]
        rows = []
        
        for user in users:
            # Colorize role
            role_text = user.role.value.upper()
            if _supports_color():
                if user.role == UserRole.ADMIN:
                    role_text = _colorize(role_text, "35")  # Magenta for admin
                elif user.role == UserRole.USER:
                    role_text = _colorize(role_text, "36")  # Cyan for user
                else:
                    role_text = _colorize(role_text, "37")  # White for guest
            
            # Colorize active status
            active_text = "YES" if user.is_active else "NO"
            if _supports_color():
                if user.is_active:
                    active_text = _colorize(active_text, "32")  # Green for active
                else:
                    active_text = _colorize(active_text, "31")  # Red for inactive
            
            rows.append([
                user.id,
                user.username,
                user.email,
                user.full_name or "-",
                role_text,
                active_text,
                user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "-"
            ])
        
        typer.echo(tabulate(rows, headers=headers, tablefmt="github"))
        typer.echo(f"\nTotal: {len(users)} users")
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error listing users: {e}", err=True)
        raise typer.Exit(1)


@app.command("create")
def create_user(
    username: str = typer.Argument(..., help="Username"),
    email: str = typer.Argument(..., help="Email address"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="Password (will prompt if not provided)"),
    full_name: Optional[str] = typer.Option(None, "--name", "-n", help="Full name"),
    role: str = typer.Option("user", "--role", "-r", help="User role (user, admin, guest)"),
    admin: bool = typer.Option(False, "--admin", help="Make user an admin"),
    inactive: bool = typer.Option(False, "--inactive", help="Create user as inactive"),
):
    """Create a new user."""
    try:
        # Get password if not provided
        if not password:
            password = getpass.getpass("Password: ")
            password_confirm = getpass.getpass("Confirm password: ")
            if password != password_confirm:
                typer.echo("Passwords do not match!", err=True)
                raise typer.Exit(1)
        
        # Determine role
        if admin:
            user_role = UserRole.ADMIN
        else:
            try:
                user_role = UserRole(role.lower())
            except ValueError:
                typer.echo(f"Invalid role: {role}. Use 'user', 'admin', or 'guest'.", err=True)
                raise typer.Exit(1)
        
        # Create user
        db = get_configured_db()
        user_data = UserCreate(
            username=username,
            email=email,
            password=password,
            full_name=full_name,
            role=user_role,
            is_active=not inactive
        )
        
        created_user = db.create_user(user_data)
        
        typer.echo("✓ User created successfully:")
        typer.echo(f"  ID: {created_user.id}")
        typer.echo(f"  Username: {created_user.username}")
        typer.echo(f"  Email: {created_user.email}")
        typer.echo(f"  Role: {created_user.role.value}")
        typer.echo(f"  Active: {'Yes' if created_user.is_active else 'No'}")
        
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error creating user: {e}", err=True)
        raise typer.Exit(1)


@app.command("delete")
def delete_user(
    username: str = typer.Argument(..., help="Username to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Delete a user."""
    try:
        db = get_configured_db()
        
        # Get user
        user = db.get_user_by_username(username)
        if not user:
            typer.echo(f"User '{username}' not found.", err=True)
            raise typer.Exit(1)
        
        # Confirm deletion
        if not force:
            confirm = typer.confirm(f"Delete user '{username}' (ID: {user.id})?")
            if not confirm:
                typer.echo("Cancelled.")
                raise typer.Exit(0)
        
        # Delete user
        if db.delete_user(user.id):
            typer.echo(f"✓ User '{username}' deleted successfully.")
        else:
            typer.echo(f"Failed to delete user '{username}'.", err=True)
            raise typer.Exit(1)
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error deleting user: {e}", err=True)
        raise typer.Exit(1)


@app.command("update")
def update_user(
    username: str = typer.Argument(..., help="Username to update"),
    email: Optional[str] = typer.Option(None, "--email", "-e", help="New email address"),
    full_name: Optional[str] = typer.Option(None, "--name", "-n", help="New full name"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="New password"),
    role: Optional[str] = typer.Option(None, "--role", "-r", help="New role (user, admin, guest)"),
    activate: bool = typer.Option(False, "--activate", help="Activate user"),
    deactivate: bool = typer.Option(False, "--deactivate", help="Deactivate user"),
):
    """Update user information."""
    try:
        db = get_configured_db()
        
        # Get user
        user = db.get_user_by_username(username)
        if not user:
            typer.echo(f"User '{username}' not found.", err=True)
            raise typer.Exit(1)
        
        # Build update data
        update_data = UserUpdate()
        
        if email:
            update_data.email = email
        if full_name:
            update_data.full_name = full_name
        if password:
            update_data.password = password
        if role:
            try:
                update_data.role = UserRole(role.lower())
            except ValueError:
                typer.echo(f"Invalid role: {role}. Use 'user', 'admin', or 'guest'.", err=True)
                raise typer.Exit(1)
        if activate:
            update_data.is_active = True
        elif deactivate:
            update_data.is_active = False
        
        # Update user
        updated_user = db.update_user(user.id, update_data)
        
        if updated_user:
            typer.echo(f"✓ User '{username}' updated successfully.")
            typer.echo(f"  Email: {updated_user.email}")
            typer.echo(f"  Full Name: {updated_user.full_name or '-'}")
            typer.echo(f"  Role: {updated_user.role.value}")
            typer.echo(f"  Active: {'Yes' if updated_user.is_active else 'No'}")
        else:
            typer.echo(f"Failed to update user '{username}'.", err=True)
            raise typer.Exit(1)
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error updating user: {e}", err=True)
        raise typer.Exit(1)


@app.command("info")
def user_info(
    username: str = typer.Argument(..., help="Username"),
):
    """Show detailed user information."""
    try:
        db = get_configured_db()
        user = db.get_user_by_username(username)
        
        if not user:
            typer.echo(f"User '{username}' not found.", err=True)
            raise typer.Exit(1)
        
        typer.echo(f"User Information: {username}")
        typer.echo(f"  ID: {user.id}")
        typer.echo(f"  Username: {user.username}")
        typer.echo(f"  Email: {user.email}")
        typer.echo(f"  Full Name: {user.full_name or '-'}")
        typer.echo(f"  Role: {user.role.value}")
        typer.echo(f"  Active: {'Yes' if user.is_active else 'No'}")
        typer.echo(f"  API Key: {'Set' if user.api_key else 'Not set'}")
        typer.echo(f"  Created: {user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else '-'}")
        typer.echo(f"  Updated: {user.updated_at.strftime('%Y-%m-%d %H:%M:%S') if user.updated_at else '-'}")
        typer.echo(f"  Last Login: {user.last_login.strftime('%Y-%m-%d %H:%M:%S') if user.last_login else 'Never'}")
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error getting user info: {e}", err=True)
        raise typer.Exit(1)


@app.command("generate-api-key")
def generate_api_key(
    username: str = typer.Argument(..., help="Username"),
):
    """Generate a new API key for a user."""
    try:
        db = get_configured_db()
        user = db.get_user_by_username(username)
        
        if not user:
            typer.echo(f"User '{username}' not found.", err=True)
            raise typer.Exit(1)
        
        api_key = db.generate_user_api_key(user.id)
        
        if api_key:
            typer.echo(f"✓ API key generated for user '{username}':")
            typer.echo(f"\n  {api_key}\n")
            typer.echo("⚠️  Save this key securely. It will not be shown again!")
        else:
            typer.echo(f"Failed to generate API key for user '{username}'.", err=True)
            raise typer.Exit(1)
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error generating API key: {e}", err=True)
        raise typer.Exit(1)


@app.command("revoke-api-key")
def revoke_api_key(
    username: str = typer.Argument(..., help="Username"),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Revoke a user's API key."""
    try:
        db = get_configured_db()
        user = db.get_user_by_username(username)
        
        if not user:
            typer.echo(f"User '{username}' not found.", err=True)
            raise typer.Exit(1)
        
        if not user.api_key:
            typer.echo(f"User '{username}' does not have an API key.", err=True)
            raise typer.Exit(1)
        
        # Confirm revocation
        if not force:
            confirm = typer.confirm(f"Revoke API key for user '{username}'?")
            if not confirm:
                typer.echo("Cancelled.")
                raise typer.Exit(0)
        
        if db.revoke_user_api_key(user.id):
            typer.echo(f"✓ API key revoked for user '{username}'.")
        else:
            typer.echo(f"Failed to revoke API key for user '{username}'.", err=True)
            raise typer.Exit(1)
        
    except typer.Exit:
        # Deliberate exits (cancel, not-found) must not be re-reported as errors
        raise
    except Exception as e:
        typer.echo(f"Error revoking API key: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
