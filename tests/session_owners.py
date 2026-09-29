"""Two users with a session each, for the plugins' access tests: alice holds
s-alice, bob s-bob, in a real SessionManager the app's session service
answers from (agent_system/auth/session_access.py asks it)."""
from __future__ import annotations

import asyncio
from datetime import datetime

from agent_system.auth.dependencies import get_optional_user
from agent_system.auth.models import User, UserRole
from agent_system.services.session_manager import SessionManager
from agent_system.services.session_service import SessionService


def user(name: str, role: UserRole = UserRole.USER) -> User:
    return User(id=1, username=name, email=f"{name}@example.com", role=role, created_at=datetime.now())


def admin() -> User:
    return user("admin", UserRole.ADMIN)


def two_users(tmp_path, monkeypatch) -> None:
    from agent_system import app as app_mod

    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    for user_id, session_id in (("alice", "s-alice"), ("bob", "s-bob")):
        session = asyncio.run(manager.create_session(user_id=user_id, session_id=session_id,
                                                     agent_name="chat", llm_profile="p"))
        session["messages"] = [{"role": "user", "content": "hi"}]
        asyncio.run(manager.save_session(session))
    monkeypatch.setattr(app_mod, "_session_service", SessionService(manager))


def viewed_by(app) -> dict:
    """Who asks, per request: ``viewer["user"] = user("alice")``; None is nobody signed in."""
    viewer = {"user": None}
    app.dependency_overrides[get_optional_user] = lambda: viewer["user"]
    return viewer
