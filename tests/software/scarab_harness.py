"""The ScarabHive under test of the browser tests (tests/software): the real app, served on a free port of
127.0.0.1 from a data folder of its own, with the users of the test data and the model's stand-in.

The test data is ``tests/software/data/*.yaml``, one key per file: ``users`` (who signs in) and ``model`` (what the
model answers, tests/agent_stand_in.py). A test names what it needs from there; a test that needs more adds an
entry to the data, not a value of its own in the test.

The app is built in this process (app.build_app with the project's config) and served by uvicorn in a thread: the
stand-in is installed at llm/registry.build_client before, so every agent's model is the stand-in, and everything
else -- the login, the sessions, the runs and their events -- is ScarabHive's own. AGENT_DATA_DIR moves every
``data/...`` path into the temporary folder: the users' database, the sessions, what plugins keep.

By hand -- to look at it, or to record a flow (``playwright codegen <url>``):

    python tests/software/scarab_harness.py
"""
from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
DATA = HERE / "data"
for _path in (str(TESTS), str(TESTS.parent / "src")):   # agent_stand_in, agent_system -- also run by hand
    if _path not in sys.path:
        sys.path.insert(0, _path)

from agent_stand_in import ModelStandIn, stand_in_for_model  # noqa: E402

#: the user a test is signed in as unless it names another
DEFAULT_USER = "tester"
START_SECONDS = 120
#: The chat takes a message once its script has wired the form (chatModule.init) and the agents are read
#: (selectorModule): a click before that sends nothing -- measured, one run in four. The one place that knows it.
CHAT_READY = ("() => !!(window.chatModule && window.chatModule.getCurrentSessionId && window.selectorModule"
              " && window.selectorModule.listState('agent') === 'ready')")


def open_chat(page: Any, path: str = "/") -> Any:
    """``page`` at ``path`` (the main page), once its chat takes a message."""
    page.goto(path)
    page.wait_for_function(CHAT_READY)
    return page


@dataclass(frozen=True)
class TestUser:
    """A user of the test data, created in the data folder of the ScarabHive under test."""

    __test__ = False  # not a test class, though pytest's name rule says Test*

    name: str
    password: str
    email: str
    role: str = "user"
    full_name: str = ""


@dataclass
class Scarab:
    """The running ScarabHive under test."""

    url: str
    data_dir: Path
    users: dict[str, TestUser]
    stand_in: ModelStandIn
    test_data: dict[str, Any]


def load_test_data(folder: Path = DATA) -> dict[str, Any]:
    """Every ``*.yaml`` of ``folder`` under its file's name: {"users": {...}, "model": {...}}."""
    return {path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) or {} for path in sorted(folder.glob("*.yaml"))}


def users_of(test_data: dict[str, Any]) -> dict[str, TestUser]:
    users = {}
    for name, entry in (test_data.get("users") or {}).items():
        unknown = set(entry) - {"password", "email", "role", "full_name"}
        if unknown:
            raise ValueError(f"test user {name}: unknown fields {sorted(unknown)}")
        users[name] = TestUser(name=name, **entry)
    return users


def create_users(users: dict[str, TestUser]) -> None:
    """The users in the user database of the data folder (AGENT_DATA_DIR must name it already)."""
    from agent_system.auth.database import UserDatabase, user_db_path
    from agent_system.auth.models import UserCreate, UserRole

    database = UserDatabase(user_db_path("data/users.db"))
    for user in users.values():
        database.create_user(UserCreate(username=user.name, email=user.email, full_name=user.full_name or None,
                                        role=UserRole(user.role), password=user.password))


def free_port() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def serve(app: Any) -> Iterator[int]:
    """``app`` on a free port of 127.0.0.1, its lifespan run; stopped on the way out."""
    import uvicorn

    sock = free_port()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="scarab-under-test", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + START_SECONDS
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the ScarabHive under test did not start (see logs/api.log)")
            time.sleep(0.05)
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


@contextmanager
def scarab_under_test(workdir: Path, monkeypatch: Any, test_data: dict[str, Any] | None = None) -> Iterator[Scarab]:
    """ScarabHive with the test data, its data in ``workdir``: the users created, the model's stand-in in every
    agent. ``monkeypatch`` (pytest's) is undone by its owner."""
    test_data = load_test_data() if test_data is None else test_data
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_DATA_DIR", str(data_dir))
    stand_in = stand_in_for_model(monkeypatch, ModelStandIn.from_data(test_data.get("model") or {}))
    users = users_of(test_data)
    create_users(users)

    from agent_system.app import build_app

    app = build_app()
    with serve(app) as port:
        yield Scarab(url=f"http://127.0.0.1:{port}", data_dir=data_dir, users=users, stand_in=stand_in,
                     test_data=test_data)


def main() -> None:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    import pytest

    with tempfile.TemporaryDirectory(prefix="scarab-under-test-") as folder, pytest.MonkeyPatch.context() as patch:
        with scarab_under_test(Path(folder), patch) as scarab:
            print(f"ScarabHive under test: {scarab.url}  (data in {scarab.data_dir})")
            for user in scarab.users.values():
                print(f"  {user.name} / {user.password}  ({user.role})")
            print("Ctrl+C ends it.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
