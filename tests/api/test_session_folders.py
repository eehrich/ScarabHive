"""The session list's folders: what the sidebar files a session by, and what moving it changes.

Driven through the real routes on a real SessionManager on disk, with the real
configuration: a session's group comes from its agent's merged config, the
folders from the plugins' own plugin.toml under the configured roots, and a
field that reached only the session file but not the index would show in no
list.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_system.api.dependencies import get_session_manager
from agent_system.api.session_endpoints import session_router
from agent_system.api.session_folders import declared_folders
from agent_system.auth.dependencies import get_optional_user
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.services.session_manager import SessionManager

WRITER_AGENT = "v6_story_coordinator"  # an agent of the group "Writer sub-agents"
CHAT_AGENT = "coder"                   # one of "Development", which no plugin's folder claims
WRITER_FOLDER = "writer_pipeline_v4"   # the plugin declaring the writer's folder
VIDEO_FOLDER = "scarab_videos"         # the plugin declaring the video folder


@pytest.fixture
def setup(tmp_path):
    config = load_settings()
    assert get_tool_server_config(WRITER_AGENT, config).metadata.category == "Writer sub-agents", \
        "fixture: the writer agent's group moved -- pick another"
    assert get_tool_server_config(CHAT_AGENT, config).metadata.category == "Development", \
        "fixture: the chat agent's group moved -- pick another"
    manager = SessionManager(storage_path=tmp_path / "sessions")
    app = FastAPI()
    app.include_router(session_router)
    app.state.config = config
    app.dependency_overrides[get_session_manager] = lambda: manager
    app.dependency_overrides[get_optional_user] = lambda: None  # no accounts: the viewer is "anonymous"
    return TestClient(app), manager, tmp_path / "sessions"


def _listed(client):
    return {s["session_id"]: s for s in client.get("/api/sessions/hierarchy").json()["sessions"]}


def _shown(client):
    return {folder["id"]: folder for folder in client.get("/api/sessions/folders").json()["folders"]}


async def test_a_session_files_by_its_agent_s_group_or_the_one_its_starter_stamped(setup):
    client, manager, _ = setup
    writer = await manager.create_session("anonymous", title="w", agent_name=WRITER_AGENT)
    chat = await manager.create_session("anonymous", title="c", agent_name=CHAT_AGENT)
    # stamped by its starter, of an agent whose own group is another -- and looked up, the writer session has it
    run = await manager.create_session("anonymous", title="r", agent_name=WRITER_AGENT)
    await manager.update_session_metadata("anonymous", run["session_id"], {"category": "Video"})
    listed = _listed(client)
    assert listed[writer["session_id"]]["category"] == "Writer sub-agents"
    assert listed[chat["session_id"]]["category"] == "Development"
    assert listed[run["session_id"]]["category"] == "Video", "the starter's stamp wins over the agent's group"
    assert all(s["folder"] is None for s in listed.values()), "nobody chose a folder yet"


def test_the_folders_are_the_ones_the_plugins_declare_none_of_the_core(setup):
    client, _, _ = setup
    shown = _shown(client)
    assert "Writer sub-agents" in shown[WRITER_FOLDER]["groups"], "writer_pipeline_v4's plugin.toml declares it"
    assert shown[VIDEO_FOLDER]["groups"] == ["Video"], "scarab_videos' plugin.toml declares it"
    assert not any("Development" in folder["groups"] for folder in shown.values()), \
        "a chat with an assistant stays out of the folders"
    assert set(shown) == {folder.id for folder in declared_folders(load_settings().plugins.plugin_dirs)}, \
        "no folder but the declared ones before the user makes one"


async def test_moving_a_session_shows_in_the_list_at_once_and_keeps_its_place_in_time(setup):
    client, manager, _ = setup
    session = await manager.create_session("anonymous", title="s", agent_name=CHAT_AGENT)
    sid = session["session_id"]
    before = _listed(client)[sid]["updated_at"]
    assert client.put(f"/api/sessions/{sid}/folder", json={"folder": VIDEO_FOLDER}).status_code == 200
    moved = _listed(client)[sid]
    assert moved["folder"] == VIDEO_FOLDER, "the index carries the folder without another save"
    assert moved["updated_at"] == before, "moved is not worked in: it keeps its day in the list"
    assert client.put(f"/api/sessions/{sid}/folder", json={"folder": ""}).status_code == 200
    assert _listed(client)[sid]["folder"] == "", "out of every folder, its group's one included"
    assert client.put(f"/api/sessions/{sid}/folder", json={"folder": None}).status_code == 200
    assert _listed(client)[sid]["folder"] is None, "back to the folder its group files it in"
    assert client.put(f"/api/sessions/{sid}/folder", json={"folder": "nope"}).status_code == 400
    assert client.put("/api/sessions/someone_elses/folder", json={"folder": ""}).status_code == 404


async def test_tags_set_on_a_session_reach_the_list_without_another_save(setup):
    client, manager, _ = setup
    session = await manager.create_session("anonymous", title="t", agent_name=CHAT_AGENT)
    await manager.update_session_metadata("anonymous", session["session_id"], {"tags": ["x"]})
    assert _listed(client)[session["session_id"]]["tags"] == ["x"]


def test_a_user_keeps_their_own_changes_and_deletions_and_gets_what_they_left_from_the_plugin(setup):
    client, _, root = setup
    shown = _shown(client)
    video, writer = shown[VIDEO_FOLDER], shown[WRITER_FOLDER]
    mine = {"id": "proj", "name": " Project ", "icon": "rocket", "groups": []}
    renamed = {**writer, "name": "Books", "icon": "star"}
    answer = client.put("/api/sessions/folders", json={"folders": [renamed, mine]})  # the video folder left out
    assert answer.status_code == 200
    shown = _shown(client)
    assert VIDEO_FOLDER not in shown, "a declared folder the user deleted stays deleted"
    assert shown[WRITER_FOLDER]["name"] == "Books" and shown["proj"]["name"] == "Project"
    stored = json.loads((root / "anonymous" / ".folders.json").read_text(encoding="utf-8"))
    assert stored["removed"] == [VIDEO_FOLDER], "a dot file: the index rebuild reads no session from it"
    # a declared folder left as it is is not copied: a plugin that changes its declaration reaches the user
    client.put("/api/sessions/folders", json={"folders": [writer, video, mine]})
    stored = json.loads((root / "anonymous" / ".folders.json").read_text(encoding="utf-8"))
    assert [folder["id"] for folder in stored["folders"]] == ["proj"] and stored["removed"] == []
    # the user's own folder takes a group a declared one claims; the declared one lets it go
    claiming = {**mine, "groups": ["Video"]}
    client.put("/api/sessions/folders", json={"folders": [writer, {**video, "groups": []}, claiming]})
    client.put("/api/sessions/folders", json={"folders": [writer, claiming]})
    stored_file = root / "anonymous" / ".folders.json"
    stored = json.loads(stored_file.read_text(encoding="utf-8"))
    stored["removed"] = []  # the video folder back, as a plugin's declaration would bring it
    stored_file.write_text(json.dumps(stored), encoding="utf-8")
    shown = _shown(client)
    assert shown["proj"]["groups"] == ["Video"] and shown[VIDEO_FOLDER]["groups"] == [], \
        "a group files into one folder: the user's, not the plugin's"


def test_a_folder_list_with_a_group_twice_or_a_foreign_icon_is_refused(setup):
    client, _, _ = setup
    twice = {"folders": [{"id": "a", "name": "A", "groups": ["Video"]}, {"id": "b", "name": "B", "groups": ["Video"]}]}
    assert client.put("/api/sessions/folders", json=twice).status_code == 422
    bad_icon = {"folders": [{"id": "a", "name": "A", "icon": "<svg>"}]}
    assert client.put("/api/sessions/folders", json=bad_icon).status_code == 422


def test_a_plugin_declares_its_folder_in_its_manifest_and_a_broken_one_costs_only_its_own(tmp_path, caplog):
    for name, body in {"a_plugin": '[session_folder]\nname = "A"\nicon = "star"\ngroups = ["G"]\n',
                       "broken": '[session_folder]\nname = "B"\nicon = "<svg>"\n',
                       "silent": '[plugin]\nname = "silent"\n'}.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "plugin.toml").write_text(body, encoding="utf-8")
    declared = declared_folders([str(tmp_path)])
    assert [(f.id, f.name, f.icon, f.groups) for f in declared] == [("a_plugin", "A", "star", ["G"])]
    assert "broken" in caplog.text, "a broken declaration is said, not swallowed"


def _plugins(root, manifests):
    for name, body in manifests.items():
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "plugin.toml").write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return declared_folders([str(root)])


def test_a_declared_folder_shown_short_of_a_group_another_files_is_saved_as_untouched(tmp_path):
    from agent_system.api.session_folders import SessionFolders, read_folders, write_folders

    store = tmp_path / "sessions"
    declared = _plugins(tmp_path / "v1", {
        "pa": '[session_folder]\nname = "A"\ngroups = ["G"]\n',
        "pb": '[session_folder]\nname = "B"\ngroups = ["G", "H"]\n'})
    shown = read_folders(store, "ann", declared)
    assert [f.groups for f in shown.folders] == [["G"], ["H"]], "fixture: the second plugin shown without G"
    mine = {"id": "mine", "name": "Mine", "icon": "star", "groups": []}
    # the viewer makes a folder: the list they save holds B as shown, short of G
    write_folders(store, "ann", SessionFolders.model_validate(
        {"folders": [*(f.model_dump() for f in shown.folders), mine]}), declared)
    stored = json.loads((store / "ann" / ".folders.json").read_text(encoding="utf-8"))
    assert [f["id"] for f in stored["folders"]] == ["mine"], "B was not touched: it follows its plugin"
    # the first plugin lets G go: B files it again
    later = _plugins(tmp_path / "v2", {"pa": '[session_folder]\nname = "A"\ngroups = []\n',
                                       "pb": '[session_folder]\nname = "B"\ngroups = ["G", "H"]\n'})
    assert {f.id: f.groups for f in read_folders(store, "ann", later).folders}["pb"] == ["G", "H"]


def test_a_broken_declaration_or_a_hand_edited_file_never_costs_the_whole_list(tmp_path):
    from agent_system.api.session_folders import read_folders

    declared = _plugins(tmp_path / "roots", {
        "twice": '[session_folder]\nname = "T"\ngroups = ["Video", "Video", " Video"]\n',
        "latin1": '[session_folder]\nname = "Grü"\n'.encode("latin-1"),
        "fine": '[session_folder]\nname = "F"\ngroups = ["X"]\n'})
    assert {f.id: f.groups for f in declared} == {"twice": ["Video"], "fine": ["X"]}, \
        "a group named twice is one; a manifest not in UTF-8 costs only its own folder"
    store = tmp_path / "sessions"
    (store / "ann").mkdir(parents=True)
    (store / "ann" / ".folders.json").write_text(json.dumps({"folders": [
        {"id": "d", "name": "D1"}, {"id": "d", "name": "D2"}]}), encoding="utf-8")
    shown = read_folders(store, "ann", declared)
    assert [f.name for f in shown.folders if f.id == "d"] == ["D1"], "an id twice: the first stands"


def test_what_the_user_changes_in_a_declared_folder_is_what_stays(tmp_path):
    from agent_system.api.session_folders import SessionFolders, read_folders, write_folders

    store = tmp_path / "sessions"
    declared = _plugins(tmp_path / "roots", {
        "pa": '[session_folder]\nname = "A"\ngroups = ["G"]\n',
        "pb": '[session_folder]\nname = "B"\ngroups = ["G", "H"]\n',
        "pc": '[session_folder]\nname = "C"\ngroups = ["Video", "Animation"]\n'})
    save = lambda folders: write_folders(store, "ann", SessionFolders.model_validate({"folders": folders}), declared)  # noqa: E731
    # B takes G from A, as the dialog sends it: A without G, B with it
    save([{"id": "pa", "name": "A", "groups": []}, {"id": "pb", "name": "B", "groups": ["G", "H"]},
          {"id": "pc", "name": "C", "groups": ["Animation", "Video"]}])  # C saved as the dialog sorts its groups
    shown = {f.id: f.groups for f in read_folders(store, "ann", declared).folders}
    assert shown["pb"] == ["G", "H"] and shown["pa"] == [], "the user gave G to B: it stays there"
    stored = json.loads((store / "ann" / ".folders.json").read_text(encoding="utf-8"))
    assert [f["id"] for f in stored["folders"]] == ["pb"],         "B changed; A only lost G to it and C only came back in another order -- both still follow their plugin"
    # B lets G go again: A, never touched, files it as its plugin declares
    save([{"id": "pa", "name": "A", "groups": []}, {"id": "pb", "name": "B", "groups": ["H"]},
          {"id": "pc", "name": "C", "groups": ["Animation", "Video"]}])
    assert {f.id: f.groups for f in read_folders(store, "ann", declared).folders}["pa"] == [],         "a group the user unticked in A stays unticked"
    # G back to A, B short of it again: the state the plugins declare -- nothing of it is the user's
    save([{"id": "pa", "name": "A", "groups": ["G"]}, {"id": "pb", "name": "B", "groups": ["H"]},
          {"id": "pc", "name": "C", "groups": ["Animation", "Video"]}])
    stored = json.loads((store / "ann" / ".folders.json").read_text(encoding="utf-8"))
    assert stored["folders"] == [], "every folder back as declared: none copied"
    # a file edited by hand with one group in two of the user's folders: the first keeps it, the list still shows
    stored["folders"] += [{"id": "x1", "name": "X1", "groups": ["Q"]}, {"id": "x2", "name": "X2", "groups": ["Q"]}]
    (store / "ann" / ".folders.json").write_text(json.dumps(stored), encoding="utf-8")
    shown = {f.id: f.groups for f in read_folders(store, "ann", declared).folders}
    assert shown["x1"] == ["Q"] and shown["x2"] == []


def test_the_limit_counts_the_user_s_own_folders_not_the_declared_ones(setup):
    from agent_system.api.session_folders import MAX_FOLDERS

    client, _, _ = setup
    declared = list(_shown(client).values())
    own = [{"id": f"f{i}", "name": f"F{i}"} for i in range(MAX_FOLDERS)]
    assert client.put("/api/sessions/folders", json={"folders": declared + own}).status_code == 200,         "a user at the limit still saves the list they see, the declared folders in it"
    too_many = own + [{"id": "one-more", "name": "One more"}]
    assert client.put("/api/sessions/folders", json={"folders": declared + too_many}).status_code == 422
