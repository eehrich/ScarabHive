"""An API-key login waits once an hour for its last_login write. What it answers is the account as it is after that
wait: a demotion or deactivation that landed meanwhile must not leave the request acting with the old role."""
from __future__ import annotations

from types import SimpleNamespace

from agent_system.auth import dependencies
from agent_system.auth.database import UserDatabase
from agent_system.auth.models import UserCreate, UserRole, UserUpdate


async def test_an_account_demoted_during_the_login_wait_answers_as_it_is_now(tmp_path):
    db = UserDatabase(tmp_path / "users.db")
    ada = db.create_user(UserCreate(username="ada", email="ada@example.com", password="correct-horse",
                                    role=UserRole.ADMIN))
    key = db.generate_user_api_key(ada.id)
    write_login = db.update_last_login

    def demoted_meanwhile(user_id):  # another request demotes and deactivates her while this one waits
        db.update_user(ada.id, UserUpdate(role=UserRole.GUEST, is_active=False))
        write_login(user_id)
    db.update_last_login = demoted_meanwhile

    user = await dependencies._authenticate(SimpleNamespace(state=SimpleNamespace(), cookies={}), None, key, db,
                                            bearer_keys=False)

    assert (user.role, user.is_active) == (UserRole.GUEST, False)
