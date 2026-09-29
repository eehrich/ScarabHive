"""network.remote_paths: a remote client reaches the listed paths only, this
machine everything (auth/remote_paths.py). Opened for a webhook, the API must
not offer its self-registration to the whole network."""
import pytest

from agent_system.auth.remote_paths import RemotePathGuard, is_local
from agent_system.config.models import NetworkConfig

WEBHOOK = "/plugins/forge/webhook"


async def call(client, path, kind="http"):
    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": kind, "path": path, "method": "POST", "headers": [], "query_string": b"",
             "client": client}
    await RemotePathGuard(inner, [WEBHOOK])(scope, receive, send)
    status = next((m.get("status") for m in sent if m["type"] == "http.response.start"), None)
    return bool(reached), status, sent


@pytest.mark.parametrize("client,path,passes", [
    (("192.0.2.27", 5000), WEBHOOK, True),
    (("192.0.2.27", 5000), "/auth/register", False),
    (("192.0.2.27", 5000), "/plugins/forge/webhook/", False),          # exact, not a prefix
    (("192.0.2.27", 5000), "/plugins/forge/webhook/../../auth/register", False),
    (("192.0.2.27", 5000), "/health", False),
    (("10.0.0.9", 5000), "/", False),
    (("127.0.0.1", 5000), "/auth/register", True),                       # this machine: unchanged
    (("::1", 5000), "/agents", True),
    (None, "/agents", True),                                              # no network peer
    (("testclient", 50000), "/agents", True),                             # Starlette's TestClient: in-process
])
async def test_remote_clients_reach_only_the_listed_paths(client, path, passes):
    reached, status, _ = await call(client, path)
    assert reached is passes and (status is None if passes else status == 404)


async def test_a_remote_websocket_is_closed():
    reached, _, sent = await call(("192.0.2.27", 5000), "/ws", kind="websocket")
    assert not reached and sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_passes():
    assert is_local({"type": "lifespan"})


def test_unset_restricts_nothing():
    assert NetworkConfig().remote_paths is None


def test_install_adds_the_guard_only_when_paths_are_set():
    from fastapi import FastAPI

    from agent_system.auth.remote_paths import install

    guarded, open_ = FastAPI(), FastAPI()
    install(guarded, NetworkConfig(remote_paths=[WEBHOOK]))
    install(open_, NetworkConfig())
    assert [m.cls for m in guarded.user_middleware] == [RemotePathGuard] and open_.user_middleware == []


def test_build_app_installs_the_guard_last():
    """add_middleware prepends: only the last registration is the outermost layer."""
    import ast
    import inspect

    from agent_system import app as app_module

    body = next(n for n in ast.parse(inspect.getsource(app_module)).body
                if isinstance(n, ast.FunctionDef) and n.name == "build_app").body
    *_, install_call, ret = body
    assert isinstance(ret, ast.Return) and ast.unparse(ret) == "return app"
    assert ast.unparse(install_call) == "install_remote_path_guard(app, config.network)"
