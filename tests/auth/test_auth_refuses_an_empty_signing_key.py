"""set_jwt_config takes no signing key everyone knows: none, or the model's default.

jose signs with "" as with any other key, so everyone could sign a login; an unset ${VAR} in auth.secret_key ends up
there (found by agentsystem-c3), an auth section without the line at the model's default, which stands in the
repository. The API sets its key here at start (app.build_app, before its middleware and routes exist), so the error
stops the start; that wiring is app.py's, not measured here.
"""
from unittest.mock import Mock

import pytest

from agent_system.auth import security
from agent_system.config.models import AuthConfig


@pytest.fixture(autouse=True)
def restore_security(monkeypatch):
    for name in ("SECRET_KEY", "ALGORITHM", "ACCESS_TOKEN_EXPIRE_MINUTES", "REFRESH_TOKEN_EXPIRE_DAYS"):
        monkeypatch.setattr(security, name, getattr(security, name))  # restored after the test
    monkeypatch.setattr(security, "AUTH_ENFORCED", False)


@pytest.mark.parametrize("key", ["", "   ", AuthConfig().secret_key], ids=["empty", "blank", "model default"])
def test_a_key_everyone_knows_is_refused_logged_and_nothing_is_set(monkeypatch, key):
    log = Mock()
    monkeypatch.setattr(security, "logger", log)
    before = security.SECRET_KEY

    with pytest.raises(ValueError, match="secret_key"):
        security.set_jwt_config(secret_key=key)

    assert security.SECRET_KEY == before
    assert security.AUTH_ENFORCED is False
    log.critical.assert_called_once()  # the API log says why it did not start, not only the console


def test_a_key_is_taken():
    security.set_jwt_config(secret_key="k" * 40)

    assert (security.SECRET_KEY, security.AUTH_ENFORCED) == ("k" * 40, True)
