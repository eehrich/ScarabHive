"""The Agent Editor's file work against a config tree under tmp_path: in-place writes, conflicts, rollback, create,
delete, sub-agent access. One read-only test runs against the real config (dry runs only, nothing is written)."""
from __future__ import annotations

import difflib
import logging
import os
import stat
import threading
from pathlib import Path

import pytest

from agent_system.config.settings import config_files, get_tool_server_config, load_settings
from plugins.agent_editor.store import CREATED_HEADER, Store, StoreError, splice, version_of

REPO = Path(__file__).resolve().parents[4]

CONFIG = """\
# Master config of the editor tests.
name: "editor-test"
includes:
  - llm.yaml
  - plugins.yaml
{agents}  - ../src/plugins*/*/agents/*.yaml
auth:
  enabled: false
"""

LLM = """\
llm_system:
  default_profile: fast
  models:
    m-fast: {provider: ollama, model: fast-model}
    m-slow: {provider: ollama, model: slow-model}
  profiles:
    fast: {model_ref: m-fast, description: Fast one}
    slow: {model_ref: m-slow}
"""

PLUGINS = """\
# Plugin servers of the editor tests.
plugins:
  plugin_dirs:
    - "{plugin_dir}"
  default_config:
    type: basic_agent
    enabled: false
    agent_config:
      llm_profile: [fast]
      system_template: "config/prompts/system_prompt.md"
      tools:
        allowed: ["files/*"]

  servers:
    # the team's manager
    team_sam:
      type: sub_agent_manager
      enabled: true
      allowed_agents:
        - writer   # the writer may be spawned
        - helper   # and the helper
      hook_config:
        enabled: false

    # a manager that allows everyone but the critic
    open_sam:
      type: sub_agent_manager
      enabled: true
      allowed_agents: ["*"]
      blocked_agents: [critic]

    # inherits team_sam's list and adds to it
    child_sam:
      type: team_sam
      enabled: true
      allowed_agents: ["+critic"]

    # inherits team_sam's list, has none of its own
    plain_sam:
      type: team_sam
      enabled: true

    # inherits open_sam's block list and adds to it
    blocking_sam:
      type: open_sam
      enabled: true
      blocked_agents: ["+writer"]

    files:
      type: file_ops
      enabled: true
    files_agent:
      type: file_ops
      enabled: true
    files_off:
      type: file_ops
"""

# CRLF on purpose, with lines ruamel would rewrite (trailing blanks, `null`, a spaced flow list).
TEAM = (
    "# Team agents: every comment here must survive a save.\n"
    "plugins:\n"
    "  servers:\n"
    "    # --- the writer ---\n"
    "    writer:\n"
    "      type: basic_agent\n"
    "      enabled: true\n"
    '      description: "Writes things"   # inline comment\n'
    '      api_hint: "Bearer ${AGENT_EDITOR_TEST_KEY}"\n'
    "      metadata:\n"
    "        visibility: ui\n"
    "        tags: [prose,  draft]\n"
    "      agent_config:\n"
    "        llm_profile: [fast, slow]\n"
    "        max_steps: 30   \n"
    "        # tools the writer needs\n"
    "        tools:\n"
    "          allowed:\n"
    '            - "files/*"\n'
    '            - "notes"\n'
    "        system_template: ./prompts/writer.md\n"
    "      spare: null   \n"
    "\n"
    "    # --- the critic inherits from the writer ---\n"
    "    critic:\n"
    "      type: writer\n"
    "      enabled: true\n"
    "      agent_config:\n"
    "        llm_params: {temperature: 0.5}\n"
    "        tools:\n"
    "          allowed:\n"
    "            # search, on top of the writer's tools\n"
    '            - "+search/*"\n'
    "\n"
    "    # --- a helper nobody inherits from ---\n"
    "    helper:\n"
    "      type: basic_agent\n"
    "      enabled: false   \n"
).replace("\n", "\r\n")[:-2]  # and no newline at the end

TWIN = """\
plugins:
  servers:
    twin:
      type: basic_agent
      description: "{side}"
"""

DEMO = """\
plugins:
  servers:
    demo_agent:
      type: basic_agent
      enabled: false
      agent_config:
        system_template: ./prompts/demo.md
"""


def build_tree(root: Path, include_agents: bool = True) -> Path:
    """A small config tree like the real one: includes, default_config, managers, an inheriting child, comments."""
    files = {
        "config/config.yaml": CONFIG.format(agents="  - agents*/*.yaml\n" if include_agents else ""),
        "config/llm.yaml": LLM,
        "config/plugins.yaml": PLUGINS.format(plugin_dir=(REPO / "src" / "plugins").as_posix()),
        "config/prompts/system_prompt.md": "You are {{ name }}.\n",
        "config/agents/team.yaml": TEAM,
        "config/agents/prompts/writer.md": "Write.\n",
        "config/agents/twin_a.yaml": TWIN.format(side="a"),
        "config/agents/twin_b.yaml": TWIN.format(side="b"),
        "src/plugins/demo/agents/demo.yaml": DEMO,
        "src/plugins/demo/agents/prompts/demo.md": "Demo.\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root / "config" / "config.yaml"


@pytest.fixture
def tree(tmp_path):
    return build_tree(tmp_path)


@pytest.fixture
def store(tree, tmp_path):
    return Store(tree, tmp_path)


def team(tmp_path) -> Path:
    return tmp_path / "config" / "agents" / "team.yaml"


def changed_lines(before: bytes, after: bytes) -> tuple[list[str], list[str]]:
    """(removed, added) lines, compared with their line endings."""
    old, new = before.decode().splitlines(keepends=True), after.decode().splitlines(keepends=True)
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += old[i1:i2]
            added += new[j1:j2]
    return removed, added


def test_the_fixture_needs_the_merge_and_loads(store, tmp_path):
    snap = store.snapshot()
    assert not snap.errors and not snap.resolve_errors
    assert {"writer", "critic", "helper", "twin", "demo_agent", "team_sam"} <= set(snap.config.plugins.servers)
    assert get_tool_server_config("critic", snap.config).agent_config.tools.allowed == ["files/*", "notes", "search/*"]
    # the file is not reproduced by a plain round-trip, so a save has to splice
    from plugins.agent_editor.store import dump, load
    src = team(tmp_path).read_bytes().decode().replace("\r\n", "\n")
    assert all(dump(load(src), indent) != src for indent in [(2, 4, 2), (2, 2, 0)])


def test_a_save_changes_only_the_edited_lines(store, tmp_path):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own("writer")
    entry["agent_config"]["max_steps"] = 45
    entry["agent_config"]["tools"]["allowed"].append("search/*")
    entry["agent_config"]["fallback_recovery_seconds"] = 60
    del entry["spare"]
    result = store.save("writer", entry, version_of(before), dry_run=False)
    after = path.read_bytes()
    assert result["version"] == version_of(after)
    removed, added = changed_lines(before, after)
    assert removed == ["        max_steps: 30   \r\n", "      spare: null   \r\n"]
    assert added == ["        max_steps: 45\r\n", '            - search/*\r\n', "        fallback_recovery_seconds: 60\r\n"]
    assert "\n" not in after.replace(b"\r\n", b"").decode()
    assert result["diff"].startswith("--- a/config/agents/team.yaml\n+++ b/config/agents/team.yaml\n")
    assert "+        max_steps: 45\n" in result["diff"]
    assert store.own("writer") == entry


def test_replacing_a_list_keeps_the_comment_that_follows_it(store, tmp_path):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own("critic")
    entry["agent_config"]["tools"]["allowed"] = ["+search/*", "+web/*"]
    store.save("critic", entry, version_of(before), dry_run=False)
    removed, added = changed_lines(before, path.read_bytes())
    assert removed == [] and added == ["            - +web/*\r\n"]
    assert b"    # --- a helper nobody inherits from ---\r\n" in path.read_bytes()


@pytest.mark.parametrize("name, change, removed, added, after", [
    # a key added to an entry goes above the comment that opens the next entry
    ("writer", lambda e: e.update(category_note="x"), [], ["      category_note: x\r\n"], "      spare: null   \r\n"),
    ("critic", lambda e: e["agent_config"]["tools"].update(blocked=["files/x"]),
     [], ["          blocked:\r\n", "            - files/x\r\n"], '            - "+search/*"\r\n'),
    # a neighbour that ruamel would reformat stays as it is
    ("writer", lambda e: e["metadata"].update(category="c"), [], ["        category: c\r\n"], "        tags: [prose,  draft]\r\n"),
    ("writer", lambda e: e["agent_config"].update(timeouts={"session_lock_timeout": 2.0}),
     [], ["        timeouts:\r\n", "          session_lock_timeout: 2.0\r\n"], "        system_template: ./prompts/writer.md\r\n"),
    # the last line of the file had no newline: it gets one, and nothing else changes
    ("helper", lambda e: e.update(category_note="x"),
     ["      enabled: false   "], ["      enabled: false   \r\n", "      category_note: x\r\n"], "      type: basic_agent\r\n"),
])
def test_an_added_key_lands_in_its_block_and_leaves_the_neighbours(store, tmp_path, name, change, removed, added, after):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own(name)
    change(entry)
    store.save(name, entry, version_of(before), dry_run=False)
    written = path.read_bytes()
    assert changed_lines(before, written) == (removed, added)
    assert "".join([after, *added]) in written.decode()
    assert store.own(name) == entry


def test_splice_keeps_a_comment_when_ruamel_dropped_a_blank_line_before_it():
    """ruamel drops whitespace-only lines between keys, so the stretch around the edit has fewer lines in its
    rendering than in the file; the comment that closed it must still stay."""
    src = "a:\n  - x\n   \n# next\nb: 1\n"
    base = "a:\n  - x\n# next\nb: 1\n"
    new = "a:\n  - x\n  - y\nb: 1\n"  # the replaced list lost the comment hung on its last item
    assert splice(src, base, new) == "a:\n  - x\n  - y\n   \n# next\nb: 1\n"


def test_splice_does_not_repeat_a_comment_ruamel_indented_anew():
    src = base = "k:\n  # note\nx: 1\n"
    new = "k:\n    # note\nx: 2\n"
    assert splice(src, base, new) == new


def test_saving_the_unchanged_entry_writes_nothing(store, tmp_path):
    path = team(tmp_path)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    for dry_run in (True, False):
        result = store.save("writer", store.own("writer"), version_of(before), dry_run=dry_run)
        assert result["diff"] == ""
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


def test_a_stale_version_is_a_conflict(store, tmp_path):
    path = team(tmp_path)
    old_version = version_of(path.read_bytes())
    path.write_bytes(path.read_bytes() + b"# someone else\r\n")
    before = path.read_bytes()
    entry = store.own("writer") | {"description": "new"}
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, old_version, dry_run=False)
    assert refused.value.status == 409 and "changed on disk" in str(refused.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("change, message", [
    (lambda e: e["agent_config"].update(llm_profile=["fast", "nope"]), "llm profile 'nope' does not exist"),
    (lambda e: e["agent_config"]["tools"].update(allowed=["+search/*", "files/*"]), "mixes list merge syntax"),
    (lambda e: e["agent_config"].update(system_template="./prompts/missing.md"), "does not exist"),
    (lambda e: e["agent_config"].update(max_step=3), "Extra inputs are not permitted"),
])
def test_an_invalid_entry_is_rolled_back(store, tmp_path, change, message):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own("writer")
    change(entry)
    assert store.save("writer", entry, version_of(before), dry_run=True)["diff"]  # a dry run does not validate
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, version_of(before), dry_run=False)
    assert refused.value.status == 422 and message in str(refused.value)
    if "mixes" in message:  # a merge error is not a cycle, and is said once
        assert str(refused.value).count(message) == 1 and "cycle" not in str(refused.value)
    assert path.read_bytes() == before
    assert "nope" not in str(store.own("writer"))


def test_a_merge_error_of_a_child_is_no_cycle(store, tmp_path):
    before = team(tmp_path).read_bytes()
    entry = store.own("critic")
    entry["agent_config"]["tools"]["allowed"] = ["+search/*", "files/*"]
    with pytest.raises(StoreError) as refused:
        store.save("critic", entry, version_of(before), dry_run=False)
    assert refused.value.status == 422
    assert str(refused.value).count("mixes list merge syntax") == 1 and "cycle" not in str(refused.value)
    assert team(tmp_path).read_bytes() == before


def test_a_parent_edit_that_breaks_a_child_is_rolled_back(store, tmp_path):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own("writer")
    # Valid for the writer; the critic's own flat llm_params then merge into profile-keyed ones.
    entry["agent_config"]["llm_params"] = {"slow": {"max_tokens": 50}}
    assert store.problems(store.snapshot().config, ["writer", "critic"]) == set()
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, version_of(before), dry_run=False)
    assert refused.value.status == 422 and str(refused.value).startswith("critic: ")
    assert path.read_bytes() == before


def test_a_string_that_the_loader_reads_as_boolean_stays_a_string(store, tmp_path):
    path = team(tmp_path)
    entry = store.own("helper") | {"description": "yes", "notes": "multi\nline"}
    store.save("helper", entry, version_of(path.read_bytes()), dry_run=False)
    assert store.own("helper") == entry
    assert load_settings(str(store.config_path)).plugins.servers["helper"].description == "yes"


def test_an_entry_in_two_files_is_read_only(store, tmp_path):
    assert store.readonly_reason("twin") == "defined in 2 files: config/agents/twin_a.yaml, config/agents/twin_b.yaml"
    assert store.readonly_reason("writer") is None
    before = (tmp_path / "config/agents/twin_a.yaml").read_bytes()
    with pytest.raises(StoreError) as refused:
        store.save("twin", {"type": "basic_agent"}, version_of(before), dry_run=False)
    assert refused.value.status == 400
    assert (tmp_path / "config/agents/twin_a.yaml").read_bytes() == before


def test_create_writes_an_included_file(store, tmp_path):
    entry = {"type": "writer", "enabled": True, "description": "A second writer"}
    dry = store.create("scribe", entry, None, dry_run=True)
    path = tmp_path / "config/agents/scribe.yaml"
    assert dry["file"] == "config/agents/scribe.yaml" and not path.exists()
    assert dry["diff"].startswith("--- a/config/agents/scribe.yaml\n+++ b/config/agents/scribe.yaml\n")
    result = store.create("scribe", entry, None, dry_run=False)
    assert path.read_text(encoding="utf-8").startswith(f"# {CREATED_HEADER}\nplugins:\n  servers:\n    scribe:\n")
    assert result["version"] == version_of(path.read_bytes())
    assert path.resolve() in [p.resolve() for p in config_files(str(store.config_path))]
    resolved = get_tool_server_config("scribe", load_settings(str(store.config_path)))
    assert resolved.agent_config.max_steps == 30 and resolved.description == "A second writer"


@pytest.mark.parametrize("name, status", [
    ("Scribe", 422), ("x", 422), ("_hidden", 422), ("scribe-2", 422), ("scribe\n", 422),
    ("writer", 400), ("team_sam", 400), ("file_ops", 400), ("basic_agent", 400),
])
def test_create_refuses_bad_and_taken_names(store, tmp_path, name, status):
    with pytest.raises(StoreError) as refused:
        store.create(name, {"type": "basic_agent"}, None, dry_run=False)
    assert refused.value.status == status
    assert sorted(p.name for p in (tmp_path / "config/agents").glob("*.yaml")) == ["team.yaml", "twin_a.yaml", "twin_b.yaml"]


def test_create_refuses_an_existing_file(store, tmp_path):
    (tmp_path / "config/agents/scribe.yaml").write_text("# reserved\n", encoding="utf-8")
    with pytest.raises(StoreError) as refused:
        store.create("scribe", {"type": "basic_agent"}, None, dry_run=False)
    assert refused.value.status == 400
    assert (tmp_path / "config/agents/scribe.yaml").read_text(encoding="utf-8") == "# reserved\n"


def test_create_from_a_source_rebases_its_template(store, tmp_path):
    entry = store.own("demo_agent")
    result = store.create("demo_copy", entry, "demo_agent", dry_run=False)
    written = load_settings(str(store.config_path))
    template = get_tool_server_config("demo_copy", written).agent_config.system_template
    assert Path(template) == (tmp_path / "src/plugins/demo/agents/prompts/demo.md").resolve()
    assert "system_template: ../../src/plugins/demo/agents/prompts/demo.md" in result["diff"]
    assert store.own("demo_agent")["agent_config"]["system_template"] == "./prompts/demo.md"


def test_create_without_the_include_rolls_back(tmp_path):
    store = Store(build_tree(tmp_path, include_agents=False), tmp_path)
    with pytest.raises(StoreError) as refused:
        store.create("scribe", {"type": "basic_agent"}, None, dry_run=False)
    assert refused.value.status == 422 and "does not include agents/*.yaml" in str(refused.value)
    assert not (tmp_path / "config/agents/scribe.yaml").exists()


def test_a_created_agent_that_does_not_load_is_removed(store, tmp_path):
    with pytest.raises(StoreError) as refused:
        store.create("scribe", {"type": "basic_agent", "agent_config": {"llm_profile": "nope"}}, None, dry_run=False)
    assert refused.value.status == 422 and "'nope'" in str(refused.value)
    assert not (tmp_path / "config/agents/scribe.yaml").exists()


def test_delete_refuses_a_parent_and_names_its_children(store, tmp_path):
    before = team(tmp_path).read_bytes()
    with pytest.raises(StoreError) as refused:
        store.delete("writer", version_of(before), dry_run=False)
    assert refused.value.status == 400 and "critic" in str(refused.value)
    assert team(tmp_path).read_bytes() == before


def test_delete_removes_the_entry_and_names_managers_that_still_list_it(store, tmp_path):
    path = team(tmp_path)
    before = path.read_bytes()
    result = store.delete("helper", version_of(before), dry_run=False)
    assert result["deleted_file"] is False
    assert result["notes"] == ["team_sam still lists helper in allowed_agents"]
    removed, added = changed_lines(before, path.read_bytes())
    # the entry, the comment block above it, and the blank line that separated it
    assert removed == ["\r\n", "    # --- a helper nobody inherits from ---\r\n", "    helper:\r\n",
                       "      type: basic_agent\r\n", "      enabled: false   "] and added == []
    assert path.read_bytes().endswith(b'            - "+search/*"\r\n')
    assert "helper" not in load_settings(str(store.config_path)).plugins.servers


def test_delete_takes_the_comments_of_an_entry_along(store, tmp_path):
    path = team(tmp_path)
    text = path.read_bytes().decode()
    inside = "      # a note inside the critic\r\n"
    path.write_bytes(text.replace("      type: writer\r\n", "      type: writer\r\n" + inside).encode())
    before = path.read_bytes()
    store._key = None
    store.delete("critic", version_of(before), dry_run=False)
    removed, added = changed_lines(before, path.read_bytes())
    assert added == []
    assert removed == ["    # --- the critic inherits from the writer ---\r\n", "    critic:\r\n", "      type: writer\r\n",
                       inside, "      enabled: true\r\n", "      agent_config:\r\n",
                       "        llm_params: {temperature: 0.5}\r\n", "        tools:\r\n", "          allowed:\r\n",
                       "            # search, on top of the writer's tools\r\n", '            - "+search/*"\r\n', "\r\n"]


def test_a_file_whose_last_entry_goes_keeps_its_other_content(store, tmp_path):
    path = tmp_path / "config/agents/twin_a.yaml"
    path.write_text("# twins live here\nplugins:\n  servers:\n    solo:\n      type: basic_agent\n", encoding="utf-8")
    store._key = None
    result = store.delete("solo", version_of(path.read_bytes()), dry_run=False)
    assert result["deleted_file"] is False
    assert path.read_text(encoding="utf-8") == "# twins live here\nplugins:\n  servers: {}\n"
    assert "solo" not in load_settings(str(store.config_path)).plugins.servers

    retired = "\n    # retired:\n    #   type: basic_agent\n"
    path.write_text("plugins:\n  servers:\n    solo:\n      type: basic_agent\n" + retired, encoding="utf-8")
    store._key = None
    assert store.delete("solo", version_of(path.read_bytes()), dry_run=False)["deleted_file"] is False
    assert path.read_text(encoding="utf-8") == "plugins:\n  servers: {}\n" + retired

    # a commented-out line inside the entry goes with it, and then nothing is left
    path.write_text("plugins:\n  servers:\n    solo:\n      type: basic_agent\n      # enabled: true\n", encoding="utf-8")
    store._key = None
    assert store.delete("solo", version_of(path.read_bytes()), dry_run=False)["deleted_file"] is True
    assert not path.exists()


def test_a_deeper_comment_above_a_deleted_key_belongs_to_the_entry_before(store, tmp_path):
    path = tmp_path / "config/agents/twin_a.yaml"
    path.write_text("plugins:\n  servers:\n    first:\n      type: basic_agent\n      # max_steps: 5\n"
                    "    # the second one\n    second:\n      type: basic_agent\n", encoding="utf-8")
    store._key = None
    store.delete("second", version_of(path.read_bytes()), dry_run=False)
    assert path.read_text(encoding="utf-8") == "plugins:\n  servers:\n    first:\n      type: basic_agent\n      # max_steps: 5\n"


def test_an_entry_in_a_flow_mapping_is_deleted_through_ruamel(store, tmp_path):
    path = tmp_path / "config/agents/twin_a.yaml"
    path.write_text("plugins:\n  servers: {solo: {type: basic_agent}, duo: {type: basic_agent}}\n", encoding="utf-8")
    store._key = None
    result = store.delete("solo", version_of(path.read_bytes()), dry_run=False)
    assert result["deleted_file"] is False
    servers = load_settings(str(store.config_path)).plugins.servers
    assert "solo" not in servers and "duo" in servers
    store.delete("duo", version_of(path.read_bytes()), dry_run=False)
    assert "duo" not in load_settings(str(store.config_path)).plugins.servers


def test_a_created_file_goes_when_its_agent_goes(store, tmp_path):
    result = store.create("scribe", {"type": "basic_agent"}, None, dry_run=False)
    path = tmp_path / result["file"]
    deleted = store.delete("scribe", result["version"], dry_run=False)
    assert deleted["deleted_file"] is True and not path.exists()


def test_delete_removes_a_file_that_holds_nothing_else(store, tmp_path):
    path = tmp_path / "src/plugins/demo/agents/demo.yaml"
    version = version_of(path.read_bytes())
    dry = store.delete("demo_agent", version, dry_run=True)
    assert dry["deleted_file"] is True and path.exists()
    assert dry["diff"].count("\n-") == DEMO.count("\n")
    result = store.delete("demo_agent", version, dry_run=False)
    assert result["deleted_file"] is True and not path.exists()
    assert "demo_agent" not in load_settings(str(store.config_path)).plugins.servers


def spawn(store, name, sam) -> dict:
    return next(row for row in store.spawnable(name) if row["sam"] == sam)


def plugins_file(tmp_path) -> Path:
    return tmp_path / "config" / "plugins.yaml"


def test_spawnable_lists_every_enabled_manager_with_its_rule(store):
    rows = {row["sam"]: row for row in store.spawnable("critic")}
    assert sorted(rows) == ["blocking_sam", "child_sam", "open_sam", "plain_sam", "team_sam"]
    assert (rows["team_sam"]["allowed"], rows["team_sam"]["rule"]) == (False, "not in allowed_agents")
    assert (rows["open_sam"]["allowed"], rows["open_sam"]["rule"]) == (False, "blocked_agents: critic")
    assert (rows["child_sam"]["allowed"], rows["child_sam"]["rule"]) == (True, "allowed_agents: critic")
    assert spawn(store, "writer", "open_sam")["rule"] == "allowed_agents: *"
    assert rows["team_sam"]["editable"] and rows["team_sam"]["file"] == "config/plugins.yaml"


def test_allowing_and_blocking_edits_the_manager_lists(store, tmp_path):
    path = plugins_file(tmp_path)
    before = path.read_bytes()
    store.set_spawnable("critic", "team_sam", True, version_of(before), dry_run=False)
    assert spawn(store, "critic", "team_sam")["allowed"] is True
    removed, added = changed_lines(before, path.read_bytes())
    assert removed == [] and added == ["        - critic\n"]
    assert b"        - writer   # the writer may be spawned\n" in path.read_bytes()

    store.set_spawnable("writer", "team_sam", False, version_of(path.read_bytes()), dry_run=False)
    assert spawn(store, "writer", "team_sam")["allowed"] is False
    assert store.own("team_sam")["allowed_agents"] == ["helper", "critic"]


def test_allowing_takes_the_name_off_the_blocked_list_and_blocking_puts_it_there(store, tmp_path):
    path = plugins_file(tmp_path)
    store.set_spawnable("critic", "open_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("open_sam")["blocked_agents"] == []
    assert spawn(store, "critic", "open_sam")["allowed"] is True

    store.set_spawnable("writer", "open_sam", False, version_of(path.read_bytes()), dry_run=False)
    own = store.own("open_sam")
    assert (own["allowed_agents"], own["blocked_agents"]) == (["*"], ["writer"])
    row = spawn(store, "writer", "open_sam")
    assert (row["allowed"], row["rule"]) == (False, "blocked_agents: writer")


def test_a_merge_list_stays_a_merge_list(store, tmp_path):
    path = plugins_file(tmp_path)
    store.set_spawnable("critic", "child_sam", False, version_of(path.read_bytes()), dry_run=False)
    assert "allowed_agents" not in store.own("child_sam")  # an emptied "+" list would replace the inherited one
    assert spawn(store, "writer", "child_sam")["allowed"] is True
    assert spawn(store, "critic", "child_sam")["allowed"] is False

    # without an own list the name goes in as "+name", so the inherited list keeps applying
    store.set_spawnable("critic", "child_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("child_sam")["allowed_agents"] == ["+critic"]
    # a bare own list stays bare
    store.set_spawnable("demo_agent", "team_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("team_sam")["allowed_agents"] == ["writer", "helper", "demo_agent"]
    assert spawn(store, "demo_agent", "child_sam")["allowed"] is True


def test_a_manager_without_own_lists_gets_prefixed_entries(store, tmp_path):
    path = plugins_file(tmp_path)
    store.set_spawnable("demo_agent", "plain_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("plain_sam") == {"type": "team_sam", "enabled": True, "allowed_agents": ["+demo_agent"]}
    store.set_spawnable("writer", "plain_sam", False, version_of(path.read_bytes()), dry_run=False)
    assert store.own("plain_sam")["blocked_agents"] == ["+writer"]
    rows = {name: spawn(store, name, "plain_sam")["allowed"] for name in ("writer", "helper", "demo_agent")}
    assert rows == {"writer": False, "helper": True, "demo_agent": True}


def test_an_inherited_block_is_lifted_with_a_removal_entry(store, tmp_path):
    path = plugins_file(tmp_path)
    assert spawn(store, "critic", "blocking_sam")["rule"] == "blocked_agents: critic"
    store.set_spawnable("critic", "blocking_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("blocking_sam")["blocked_agents"] == ["+writer", "!critic"]
    assert spawn(store, "critic", "blocking_sam")["allowed"] is True
    store.set_spawnable("writer", "blocking_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("blocking_sam")["blocked_agents"] == ["!critic"]
    assert spawn(store, "writer", "blocking_sam")["allowed"] is True


def test_a_merge_list_gets_a_prefixed_entry(store, tmp_path):
    path = plugins_file(tmp_path)
    store.set_spawnable("demo_agent", "child_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert store.own("child_sam")["allowed_agents"] == ["+critic", "+demo_agent"]
    assert spawn(store, "writer", "child_sam")["allowed"] is True


def test_toggling_to_the_current_state_changes_nothing(store, tmp_path):
    path = plugins_file(tmp_path)
    before = path.read_bytes()
    result = store.set_spawnable("writer", "team_sam", True, version_of(before), dry_run=False)
    assert result == {"diff": "", "version": version_of(before)}
    assert path.read_bytes() == before


def test_an_unknown_manager_is_refused(store, tmp_path):
    version = version_of(plugins_file(tmp_path).read_bytes())
    for sam in ("files", "nobody"):
        with pytest.raises(StoreError) as refused:
            store.set_spawnable("writer", sam, True, version, dry_run=False)
        assert refused.value.status == 404


# ---------------------------------------------------------------------- stale reads, rollback, checks


def test_an_edit_made_meanwhile_is_not_reverted_by_a_save_from_an_older_read(store, tmp_path):
    path = team(tmp_path)
    own, version = store.own("writer"), store.version("writer")
    foreign = path.read_bytes().replace(b"Writes things", b"Edited by hand")
    path.write_bytes(foreign)
    # read again within the snapshot's second: the same old content, and the version that belongs to it
    assert (store.own("writer"), store.version("writer")) == (own, version)
    with pytest.raises(StoreError) as refused:
        store.save("writer", {**own, "enabled": False}, store.version("writer"), dry_run=False)
    assert refused.value.status == 409
    assert path.read_bytes() == foreign
    # the conflict proves the read stale: the reload it asks for shows the disk at once, not a second later
    assert store.own("writer")["description"] == "Edited by hand"
    assert store.version("writer") == version_of(foreign)


def invalid_writer(store) -> dict:
    return {**store.own("writer"), "agent_config": {**store.own("writer")["agent_config"], "llm_profile": ["nope"]}}


def test_a_rollback_does_not_overwrite_what_someone_wrote_meanwhile(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    path = team(tmp_path)
    entry, version = invalid_writer(store), store.version("writer")
    foreign = []

    def load_and_race(config_path):
        foreign.append(b"# written by someone else\r\n" + path.read_bytes())  # someone edits our (invalid) write
        path.write_bytes(foreign[-1])
        return load_settings(config_path)

    monkeypatch.setattr(store_module, "load_settings", load_and_race)
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, version, dry_run=False)
    assert refused.value.status == 409
    assert "changed on disk while it was checked" in str(refused.value) and "config/agents/team.yaml" in str(refused.value)
    assert "llm profile 'nope' does not exist" in str(refused.value)
    assert path.read_bytes() == foreign[-1]


def test_a_sharing_violation_is_retried(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    calls = []
    real = store_module.os.replace

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky)
    entry = {**store.own("writer"), "description": "retried"}
    store.save("writer", entry, store.version("writer"), dry_run=False)
    assert len(calls) == 3 and store.own("writer")["description"] == "retried"


def test_a_file_that_cannot_be_replaced_leaves_no_temp_file(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module

    def locked(src, dst):
        os.chmod(src, stat.S_IREAD)  # the temp file carries the target's mode; a read-only one must still go
        raise PermissionError("locked")

    monkeypatch.setattr(store_module.os, "replace", locked)
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: None)
    before = team(tmp_path).read_bytes()
    with pytest.raises(StoreError) as refused:
        store.save("writer", {**store.own("writer"), "description": "x"}, version_of(before), dry_run=False)
    assert refused.value.status == 400 and "config/agents/team.yaml" in str(refused.value)
    assert team(tmp_path).read_bytes() == before
    assert sorted(p.name for p in team(tmp_path).parent.iterdir() if p.is_file()) == ["team.yaml", "twin_a.yaml", "twin_b.yaml"]


def test_a_failed_rollback_says_the_file_holds_the_rejected_content(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    real = store_module.os.replace
    calls, logged = [], []
    monkeypatch.setattr(store_module.logger, "error", lambda message, *args: logged.append(message % args))

    def first_only(src, dst):
        calls.append(dst)
        if len(calls) > 1:
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(store_module.os, "replace", first_only)
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: None)
    with pytest.raises(StoreError) as refused:
        store.save("writer", invalid_writer(store), store.version("writer"), dry_run=False)
    assert refused.value.status == 500
    assert "config/agents/team.yaml" in str(refused.value) and "holds the rejected content" in str(refused.value)
    assert "nope" in team(tmp_path).read_text(encoding="utf-8")
    assert any("team.yaml" in line for line in logged)


@pytest.mark.parametrize("failures, status", [(2, 422), (99, 500)])
def test_the_rollback_read_is_retried_and_its_failure_reported(store, tmp_path, monkeypatch, failures, status):
    from plugins.agent_editor import store as store_module
    path = team(tmp_path)
    before = path.read_bytes()
    entry, version = invalid_writer(store), store.version("writer")
    real_load, real_read = store_module.load_settings, Path.read_bytes
    denied, logged = [], []
    monkeypatch.setattr(store_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(store_module.logger, "error", lambda message, *args: logged.append(message % args))

    def held(self):
        if self == path and len(denied) < failures:
            denied.append(1)
            raise PermissionError("held by another process")
        return real_read(self)

    def load_then_hold(config_path):
        loaded = real_load(config_path)
        monkeypatch.setattr(Path, "read_bytes", held)  # from here on: the rollback's read
        return loaded

    monkeypatch.setattr(store_module, "load_settings", load_then_hold)
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, version, dry_run=False)
    assert refused.value.status == status and denied
    if status == 422:
        assert real_read(path) == before and not logged
    else:
        assert "could not be restored" in str(refused.value) and "nope" in real_read(path).decode()
        assert any("team.yaml" in line for line in logged)


def test_a_read_only_file_is_read_only(store, tmp_path):
    path = team(tmp_path)
    os.chmod(path, stat.S_IREAD)
    try:
        assert store.readonly_reason("writer") == "the file is read-only"
        with pytest.raises(StoreError) as refused:
            store.save("writer", store.own("writer"), store.version("writer"), dry_run=False)
        assert refused.value.status == 400
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("change, message", [
    (lambda e: e.update(type="no_such_plugin"), "unknown type 'no_such_plugin'"),
    (lambda e: e.update(type="critic"), "inheritance cycle"),
])
def test_an_unknown_type_or_a_cycle_is_rolled_back(store, tmp_path, change, message):
    path = team(tmp_path)
    before = path.read_bytes()
    entry = store.own("writer")
    change(entry)
    with pytest.raises(StoreError) as refused:
        store.save("writer", entry, version_of(before), dry_run=False)
    assert refused.value.status == 422 and message in str(refused.value)
    assert path.read_bytes() == before


def test_a_problem_an_entry_already_has_neither_blocks_nor_hides_another(store, tmp_path):
    path = team(tmp_path)
    broken = path.read_bytes().replace(b'      type: basic_agent\r\n      enabled: false   ',
                                       b'      type: basic_agent\r\n      agent_config:\r\n'
                                       b'        llm_profile: [nope]\r\n'
                                       b'        tools: {allowed: ["+a", "b"]}\r\n      enabled: false   ')
    path.write_bytes(broken)
    store._key = None
    assert store.problems(store.snapshot().config, ["helper"])  # the fixture: mixed list and unknown profile
    saved = store.save("helper", {**store.own("helper"), "description": "still broken"}, version_of(broken), dry_run=False)
    assert saved["diff"]
    current = path.read_bytes()
    entry = store.own("helper")
    entry["agent_config"]["system_template"] = "./prompts/missing.md"
    with pytest.raises(StoreError) as refused:
        store.save("helper", entry, version_of(current), dry_run=False)
    assert refused.value.status == 422 and "missing.md does not exist" in str(refused.value)
    assert "nope" not in str(refused.value)
    assert path.read_bytes() == current


def test_floats_and_yaml_11_words_read_back_as_written(store, tmp_path):
    entry = store.own("helper")
    entry["agent_config"] = {"llm_params": {"temperature": 1e-05}, "template_vars": {"on": "off", "y": 1, "null": 2, "~": 3}}
    store.save("helper", entry, store.version("helper"), dry_run=False)
    assert store.own("helper") == entry
    text = team(tmp_path).read_text(encoding="utf-8")
    assert "temperature: 1.0e-05" in text and "'on': 'off'" in text


def test_a_value_that_cannot_be_written_is_named(store):
    entry = store.own("helper")
    entry["agent_config"] = {"template_vars": {"ratio": float("nan")}}
    with pytest.raises(StoreError) as refused:
        store.save("helper", entry, store.version("helper"), dry_run=True)
    assert refused.value.status == 400
    assert "plugins.servers.helper.agent_config.template_vars.ratio" in str(refused.value) and "nan" in str(refused.value)


def test_a_file_included_twice_counts_once(tmp_path):
    config = build_tree(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace("  - agents*/*.yaml\n", "  - agents*/*.yaml\n  - agents/team.yaml\n"),
                      encoding="utf-8")
    store = Store(config, tmp_path)
    assert len(config_files(str(config))) > len(store.snapshot().files)
    assert store.readonly_reason("writer") is None


def test_a_file_included_again_keeps_its_last_place(tmp_path):
    config = build_tree(tmp_path)
    config.write_text(config.read_text(encoding="utf-8").replace("  - agents*/*.yaml\n", "  - agents*/*.yaml\n  - agents/twin_a.yaml\n"),
                      encoding="utf-8")
    store = Store(config, tmp_path)
    loaded = load_settings(str(config)).plugins.servers["twin"].description
    assert loaded == "a", "the fixture: the loader's winner is the file loaded last"
    names = [path.name for path in store.snapshot().files]
    assert names.count("twin_a.yaml") == 1 and names.index("twin_a.yaml") > names.index("twin_b.yaml")
    assert store.files_of("twin")[-1].name == "twin_a.yaml"
    assert store.own("twin")["description"] == loaded


def diff_body(diff: str) -> tuple[list[str], list[str]]:
    """Removed and added lines of a diff; a last line that only gains its newline counts as neither."""
    lines = diff.splitlines()[2:]
    removed = [line[1:] for line in lines if line.startswith("-")]
    added = [line[1:] for line in lines if line.startswith("+")]
    if "\\ No newline at end of file" in lines and removed and removed[-1] in added:
        added.remove(removed.pop())
    return removed, added


def test_the_real_config_every_agent_reads_and_dry_runs_touch_only_the_edit():
    """Read-only against config/: every server resolves; for every editable agent, the unchanged entry renders to an
    empty diff, and an added key to exactly that one line. Only dry runs; the tree is compared before and after."""
    from plugins.agent_editor import sources

    store = Store(REPO / "config" / "config.yaml", REPO)
    snap = store.snapshot()
    stamps = {path: path.stat().st_mtime_ns for path in snap.files}
    assert not snap.errors and not snap.resolve_errors
    classes = sources.agent_classes(None, sources.catalog_for(snap.config))
    agents = [name for name, resolved in snap.resolved.items() if resolved.type in classes]
    # ~134 with the writer's agents, ~41 in the open-source checkout, which has no src/plugins_writer.
    least = 100 if (REPO / "src" / "plugins_writer").is_dir() else 25
    assert len(agents) > least, f"the real config should hold more than {least} agents, found {len(agents)}"
    editable = [name for name in agents if store.readonly_reason(name) is None]
    assert editable, "no editable agent: the dry runs below would check nothing"
    unchanged, stray, refused = {}, {}, {}
    for name in editable:
        own, version = store.own(name), store.version(name)
        diff = store.save(name, own, version, dry_run=True)["diff"]
        if diff:
            unchanged[name] = diff
        try:
            diff = store.save(name, {**own, "agent_editor_probe": 1}, version, dry_run=True)["diff"]
        except StoreError as error:
            refused[name] = str(error)
            continue
        if diff_body(diff) != ([], ["      agent_editor_probe: 1"]):
            stray[name] = diff
    assert unchanged == {}
    assert stray == {}
    # A refusal is safe (nothing is written) but must stay the rare layout, not the rule.
    assert len(refused) <= len(editable) // 20, refused
    assert {path: path.stat().st_mtime_ns for path in snap.files} == stamps


def test_masking_puts_placeholders_back_without_rewriting_other_words():
    from types import SimpleNamespace

    from plugins.agent_editor.store import Snapshot

    snap = SimpleNamespace(secrets={"sekrit-value-123": "${KEY}", "eu": "${REGION}"})
    snap.mask = lambda value: Snapshot.mask(snap, value)
    masked = snap.mask({"auth": "Bearer sekrit-value-123", "region": "eu", "list": ["eu", "neuron", "Europe"]})
    assert masked == {"auth": "Bearer ${KEY}", "region": "${REGION}", "list": ["${REGION}", "neuron", "Europe"]}


def test_a_save_made_while_the_edit_was_rendered_is_not_overwritten(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    path = team(tmp_path)
    own, version = store.own("writer"), store.version("writer")
    foreign = path.read_bytes().replace(b"Writes things", b"Saved by hand")
    real_render = store_module.render

    def render_while_someone_saves(*args):
        path.write_bytes(foreign)
        return real_render(*args)

    monkeypatch.setattr(store_module, "render", render_while_someone_saves)
    with pytest.raises(StoreError) as refused:
        store.save("writer", {**own, "enabled": False}, version, dry_run=False)
    assert refused.value.status == 409
    assert path.read_bytes() == foreign


def test_create_never_replaces_a_file_made_meanwhile(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    target = tmp_path / "config/agents/scribe.yaml"
    real_check = store_module._check_parse

    def check_while_someone_creates(*args):
        target.write_text("# made by hand\n", encoding="utf-8")
        return real_check(*args)

    monkeypatch.setattr(store_module, "_check_parse", check_while_someone_creates)
    with pytest.raises(StoreError) as refused:
        store.create("scribe", {"type": "basic_agent", "enabled": True}, None, dry_run=False)
    assert refused.value.status == 409
    assert target.read_text(encoding="utf-8") == "# made by hand\n"


def test_create_refuses_a_config_outside_the_root(tree, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(StoreError) as refused:
        Store(tree, elsewhere).create("scribe", {"type": "basic_agent"}, None, dry_run=False)
    assert refused.value.status == 400 and "outside the repository root" in str(refused.value)
    assert not (tmp_path / "config/agents/scribe.yaml").exists()


def test_an_empty_manager_list_counts_as_unset(store, tmp_path):
    path = plugins_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("      blocked_agents: [critic]\n", "      blocked_agents:\n"),
                    encoding="utf-8")
    store.set_spawnable("critic", "open_sam", True, version_of(path.read_bytes()), dry_run=False)
    assert spawn(store, "critic", "open_sam")["allowed"] is True


def test_a_manager_list_that_is_no_list_is_left_alone(store, tmp_path):
    path = plugins_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace('allowed_agents: ["*"]', 'allowed_agents: "*"'),
                    encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(StoreError) as refused:
        store.set_spawnable("writer", "open_sam", False, version_of(before), dry_run=False)
    assert refused.value.status == 400 and "allowed_agents is not a list" in str(refused.value)
    assert path.read_bytes() == before


def test_an_entry_with_a_value_json_cannot_carry_is_not_saved_from_the_form(store, tmp_path):
    path = team(tmp_path)
    path.write_bytes(path.read_bytes().replace(b"    helper:\r\n", b"    helper:\r\n      since: 2026-09-17\r\n"))
    reason = store.form_reason("helper")
    assert reason is not None and reason.startswith("helper.since: a date") and "edit the file itself" in reason
    assert store.form_reason("writer") is None
    before = path.read_bytes()
    with pytest.raises(StoreError) as refused:
        store.save("helper", {**store.own("helper"), "since": "2026-09-17"}, store.version("helper"), dry_run=False)
    assert refused.value.status == 400 and "the form cannot carry" in str(refused.value)
    assert path.read_bytes() == before
    # the file is still writable: delete works on the parsed file, not through the form
    assert store.readonly_reason("helper") is None
    assert store.delete("helper", store.version("helper"), dry_run=True)["diff"]
    # a copy would carry the value as text
    with pytest.raises(StoreError) as refused:
        store.create("helper_copy", {**store.own("helper"), "since": "2026-09-17"}, "helper", dry_run=True)
    assert refused.value.status == 400 and "cannot be copied" in str(refused.value)


def test_a_failed_create_leaves_no_partial_file(store, tmp_path, monkeypatch):
    real_open = Path.open

    class FullDisk:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[:5])
            raise OSError(28, "No space left on device")

    def open_on_a_full_disk(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return FullDisk(handle) if mode == "xb" else handle

    real_unlink = Path.unlink
    unlinks = []

    def unlink_held_once(path, missing_ok=False):
        unlinks.append(path)
        if len(unlinks) == 1:
            raise PermissionError(13, "held by a virus scanner")
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "open", open_on_a_full_disk)
    monkeypatch.setattr(Path, "unlink", unlink_held_once)
    with pytest.raises(StoreError) as refused:
        store.create("scribe", {"type": "basic_agent", "enabled": True}, None, dry_run=False)
    assert refused.value.status == 400 and "No space left" in str(refused.value)
    assert not (tmp_path / "config/agents/scribe.yaml").exists()


def test_a_file_that_cannot_be_read_before_the_write_is_an_answer(store, tmp_path, monkeypatch):
    from plugins.agent_editor import store as store_module
    real_read = store_module._read
    reads = []

    def read_then_fail(path):
        reads.append(path)
        if len(reads) > 1:
            raise PermissionError(13, "held by another process")
        return real_read(path)

    monkeypatch.setattr(store_module, "_read", read_then_fail)
    path = team(tmp_path)
    before = path.read_bytes()
    with pytest.raises(StoreError) as refused:
        store.save("writer", {**store.own("writer"), "enabled": False}, store.version("writer"), dry_run=False)
    assert refused.value.status == 400 and "cannot be read" in str(refused.value)
    assert path.read_bytes() == before


def test_a_master_config_that_does_not_parse_is_named(store, tree):
    tree.write_text("includes: [\n", encoding="utf-8")
    with pytest.raises(StoreError) as refused:
        store.snapshot()
    assert refused.value.status == 500 and "does not load" in str(refused.value)


def test_the_editors_own_loads_do_not_repeat_the_loader_report(tree, tmp_path, caplog):
    # the loader reports the unknown profile, the resolver the cycle
    (tmp_path / "config/agents/broken.yaml").write_text(
        "plugins:\n  servers:\n    broken:\n      type: basic_agent\n      enabled: true\n"
        "      agent_config:\n        llm_profile: [nope]\n"
        "    loop_a:\n      type: loop_b\n    loop_b:\n      type: loop_a\n", encoding="utf-8")
    caplog.set_level(logging.INFO, logger=load_settings.__module__)
    reported = lambda: " ".join(record.getMessage() for record in caplog.records)  # noqa: E731
    store = Store(tree, tmp_path)
    store.snapshot()
    store.save("writer", {**store.own("writer"), "enabled": False}, store.version("writer"), dry_run=False)
    assert "nope" not in reported() and "loop_a" not in reported()
    config = load_settings(str(tree))  # the app's own load and resolving still report
    get_tool_server_config("loop_a", config)
    assert "nope" in reported() and "loop_a" in reported()


def test_the_quiet_loader_drops_only_the_records_of_its_own_thread(caplog):
    from plugins.agent_editor.store import quiet_loader
    settings_logger = logging.getLogger(load_settings.__module__)
    caplog.set_level(logging.INFO, logger=settings_logger.name)
    with quiet_loader():
        settings_logger.info("mine")
        other = threading.Thread(target=settings_logger.info, args=("theirs",))
        other.start()
        other.join()
    settings_logger.info("after")
    assert [record.getMessage() for record in caplog.records if record.name == settings_logger.name] == ["theirs", "after"]
