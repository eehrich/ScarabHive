"""The folder picker's route (agent_system/plugins/folder_routes.py): which folders of the server a panel may list.

A stand-in web class on FolderRoutes behind a plain FastAPI app -- no auth configured, so the admin gate lets the
local owner through (agent_tasks.admin_of answers "local").
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_system.plugins import folder_routes
from agent_system.plugins.folder_routes import FolderRoutes
from agent_system.utils import path_sandbox


class Web(FolderRoutes):
    def __init__(self, roots):
        self.roots = roots

    def folder_roots(self):
        return self.roots


def client_for(roots) -> TestClient:
    app = FastAPI()
    app.get("/p/dirs")(Web(roots).get_dirs)
    return TestClient(app)


def names(answer: dict) -> list[str]:
    return [one["name"] for one in answer["dirs"]]


def test_the_top_lists_the_roots_that_exist_only(tmp_path):
    there = tmp_path / "projects"
    there.mkdir()

    answer = client_for([there, tmp_path / "missing"]).get("/p/dirs").json()

    assert answer == {"path": "", "parent": None, "dirs": [{"name": str(there.resolve()), "path": str(there.resolve())}],
                      "truncated": False}


def test_with_no_root_there_the_top_is_refused_naming_the_roots(tmp_path):
    client = client_for([tmp_path / "first", tmp_path / "second"])

    english = client.get("/p/dirs")
    german = client.get("/p/dirs", headers={"Accept-Language": "de-DE,de;q=0.9"})

    assert english.status_code == 422 and german.status_code == 422
    for answer in (english, german):
        detail = answer.json()["detail"]
        assert "first" in detail and "second" in detail, detail
    assert english.json()["detail"].startswith("None of the allowed folders exists")
    assert german.json()["detail"].startswith("Keiner der erlaubten Ordner existiert")


def test_a_folder_outside_the_roots_is_refused(tmp_path):
    root = tmp_path / "root"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()

    answer = client_for([root]).get("/p/dirs", params={"path": str(elsewhere)})

    assert answer.status_code == 422
    assert "outside the allowed directories" in answer.json()["detail"]


def test_a_folder_lists_its_folders_and_leaves_hidden_and_private_ones_and_files_out(tmp_path, monkeypatch):
    root = tmp_path / "root"
    for name in ("beta", "Alpha", ".git", "sessions"):
        (root / name).mkdir(parents=True)
    (root / "notes.txt").write_text("a file", encoding="utf-8")
    # the installation's private folders, here: root/sessions
    monkeypatch.setattr(path_sandbox, "private_places",
                        lambda: (tmp_path / "config", tmp_path / "users.db", (root / "sessions",)))

    answer = client_for([root]).get("/p/dirs", params={"path": str(root)}).json()

    assert names(answer) == ["Alpha", "beta"]
    assert answer["path"] == str(root.resolve()) and answer["parent"] == "" and answer["truncated"] is False
    nested = client_for([root]).get("/p/dirs", params={"path": answer["dirs"][0]["path"]}).json()
    assert nested["parent"] == str(root.resolve()) and nested["dirs"] == []


def test_a_hidden_folder_is_not_entered_by_its_path_either(tmp_path):
    root = tmp_path / "root"
    (root / ".git" / "objects").mkdir(parents=True)
    client = client_for([root])
    for hidden in (root / ".git", root / ".git" / "objects"):
        refused = client.get("/p/dirs", params={"path": str(hidden)})
        assert refused.status_code == 422 and "hidden" in refused.json()["detail"], refused.text


@pytest.mark.skipif(os.name != "nt", reason="a junction is a Windows link")
def test_a_junction_into_a_private_folder_is_left_out(tmp_path, monkeypatch):
    """A junction is no symlink to DirEntry: it is judged where it leads, not by its own name."""
    import _winapi

    root = tmp_path / "root"
    (root / "sessions").mkdir(parents=True)
    (root / "open").mkdir()
    _winapi.CreateJunction(str(root / "sessions"), str(root / "shortcut"))
    monkeypatch.setattr(path_sandbox, "private_places",
                        lambda: (tmp_path / "config", tmp_path / "users.db", (root / "sessions",)))

    assert names(client_for([root]).get("/p/dirs", params={"path": str(root)}).json()) == ["open"]


def test_a_link_out_of_the_roots_is_left_out(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "inside").mkdir(parents=True)
    outside.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine does not let the test make a symlink")

    answer = client_for([root]).get("/p/dirs", params={"path": str(root)}).json()

    assert names(answer) == ["inside"]


def test_a_long_folder_is_cut_at_the_cap_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_routes, "MAX_DIRS", 3)
    full, over = tmp_path / "full", tmp_path / "over"
    for name in ("a", "b", "c"):
        (full / name).mkdir(parents=True)
    for name in ("a", "b", "c", "d", "e"):
        (over / name).mkdir(parents=True)
    client = client_for([full, over])

    exactly = client.get("/p/dirs", params={"path": str(full)}).json()
    cut = client.get("/p/dirs", params={"path": str(over)}).json()

    assert names(exactly) == ["a", "b", "c"] and exactly["truncated"] is False
    assert names(cut) == ["a", "b", "c"] and cut["truncated"] is True


def test_a_path_that_is_no_folder_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.txt").write_text("a file", encoding="utf-8")

    answer = client_for([root]).get("/p/dirs", params={"path": str(root / "notes.txt")})

    assert answer.status_code == 422 and "No such folder" in answer.json()["detail"]


def test_a_sandbox_of_the_plugin_serves_as_the_roots(tmp_path):
    root = tmp_path / "root"
    (root / "one").mkdir(parents=True)
    sandbox = path_sandbox.PathSandbox.from_config([str(root)], base=tmp_path)

    answer = client_for(sandbox).get("/p/dirs", params={"path": str(root)}).json()

    assert names(answer) == ["one"]


def test_the_listing_asks_for_an_admin(tmp_path, monkeypatch):
    async def refuse(request):
        raise HTTPException(status_code=403, detail="Admin privileges required")

    monkeypatch.setattr(folder_routes, "admin_of", refuse)
    (tmp_path / "root").mkdir()

    assert client_for([tmp_path / "root"]).get("/p/dirs").status_code == 403
