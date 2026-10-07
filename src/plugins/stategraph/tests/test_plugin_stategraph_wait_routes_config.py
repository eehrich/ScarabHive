"""The shipped route rules let a person answer the wait their own request runs into, and keep the machines admin
work: config/security.yaml puts the wait question's routes above the /plugins/stategraph/* rule."""

import pytest

from agent_system.config.settings import load_settings
from agent_system.plugins.web_adapter import PluginEndpointSecurityEnforcer


@pytest.fixture(scope="module")
def enforcer():
    config = load_settings()
    return PluginEndpointSecurityEnforcer(config.auth.model_copy(update={"enabled": True}))


@pytest.mark.parametrize("path,method", [("/plugins/stategraph/answer", "POST"), ("/plugins/stategraph/pending", "GET")])
def test_a_user_reaches_the_wait_question_routes(enforcer, path, method):
    policy = enforcer.get_plugin_policy("stategraph", path, method)

    assert (policy["requires_auth"], policy["min_role"]) == (True, "user"), policy


@pytest.mark.parametrize("path", ["/plugins/stategraph/", "/plugins/stategraph/api/machines",
                                  "/plugins/stategraph/answer/x", "/plugins/stategraph/answers"])
def test_the_machines_stay_for_admins(enforcer, path):
    assert enforcer.get_plugin_policy("stategraph", path, "GET")["min_role"] == "admin"
