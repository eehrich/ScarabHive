"""Keeping machines in order: a machine the author made can be deleted again, and every machine has a folder.

A saved machine lived forever: nothing removed it but a hand in the file system. And the list grew flat --
examples, the writer's machines and the author's own side by side. Delete takes the version the author saw
(no blind delete) and only what belongs to the machine alone; the folder is the machine's ``group`` or where
it comes from.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.stategraph.model.loader import SnapshotSources, load_tree
from plugins.stategraph.store import MachineStore, VersionConflict, version_of

MACHINE = "stategraph: 1\nid: {id}\ninitial: a\nstates:\n  a:\n    type: final\n"


def store_with(tmp_path: Path) -> tuple[MachineStore, Path, Path]:
    own, shipped = tmp_path / "data", tmp_path / "shipped"
    own.mkdir()
    shipped.mkdir()
    return MachineStore([str(own), str(shipped)], [str(own)], base=tmp_path), own, shipped


def test_delete_removes_the_machine_its_layout_and_the_module_it_is_given(tmp_path):
    store, own, _ = store_with(tmp_path)
    (own / "m.yaml").write_text(MACHINE.format(id="m"), encoding="utf-8")
    (own / "m.layout.json").write_text("{}", encoding="utf-8")
    (own / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (own / "other.yaml").write_text(MACHINE.format(id="other"), encoding="utf-8")

    removed = store.delete("m", expected_version=version_of(MACHINE.format(id="m")), companion=own / "m.py")

    assert sorted(Path(p).name for p in removed) == ["m.layout.json", "m.py", "m.yaml"]
    assert sorted(p.name for p in own.iterdir()) == ["other.yaml"]
    assert store.find("m") is None


def test_delete_takes_only_the_version_the_author_saw(tmp_path):
    store, own, _ = store_with(tmp_path)
    (own / "m.yaml").write_text(MACHINE.format(id="m"), encoding="utf-8")

    with pytest.raises(VersionConflict):
        store.delete("m", expected_version="an older version")

    assert (own / "m.yaml").is_file()


def test_delete_refuses_a_machine_outside_the_writable_roots_and_removes_nothing(tmp_path):
    store, own, shipped = store_with(tmp_path)
    (shipped / "ex.yaml").write_text(MACHINE.format(id="ex"), encoding="utf-8")
    (own / "m.yaml").write_text(MACHINE.format(id="m"), encoding="utf-8")
    (shipped / "shared.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        store.delete("ex", expected_version=version_of(MACHINE.format(id="ex")))
    with pytest.raises(PermissionError):  # a module outside the writable root: checked before anything goes
        store.delete("m", expected_version=version_of(MACHINE.format(id="m")), companion=shipped / "shared.py")

    assert (shipped / "ex.yaml").is_file() and (own / "m.yaml").is_file() and (shipped / "shared.py").is_file()


def service_over(tmp_path: Path, *files: tuple[str, str]):
    """The plugin's service over an own root (writable) and a plugin's machines/ folder (shipped, read-only)."""
    import types

    from plugins.stategraph.engine.journal import RunStore
    from plugins.stategraph.engine.runner import RunManager
    from plugins.stategraph.service import StateGraphService

    own, shipped = tmp_path / "data", tmp_path / "some_plugin" / "machines"
    own.mkdir()
    shipped.mkdir(parents=True)
    for where, name, text in files:
        (own if where == "own" else shipped).joinpath(name).write_text(text, encoding="utf-8")
    runs = RunStore(tmp_path / "runs.db")
    server = types.SimpleNamespace(name="stategraph", system_config=None, runner_agent="r", inject_params={},
                                   machines=MachineStore([str(own), str(shipped)], [str(own)], base=tmp_path),
                                   run_store=runs, run_manager=RunManager(runs))
    return StateGraphService(server), own, shipped


def with_python(machine_id: str, module: str) -> str:
    return MACHINE.format(id=machine_id).replace("initial: a\n", f"python: {module}\ninitial: a\n")


def test_the_list_names_each_machines_folder(tmp_path):
    service, _, _ = service_over(tmp_path, ("own", "mine.yaml", MACHINE.format(id="mine")),
                                 ("own", "grouped.yaml", MACHINE.format(id="grouped") + "group: Writer/v6\n"),
                                 ("shipped", "example.yaml", MACHINE.format(id="example")))

    groups = {m["id"]: m["group"] for m in service.list_machines()}

    assert groups == {"mine": "My machines", "grouped": "Writer/v6", "example": "some_plugin"}


def test_delete_through_the_service_removes_a_module_only_this_machine_uses(tmp_path):
    service, own, _ = service_over(
        tmp_path, ("own", "solo.yaml", with_python("solo", "solo.py")), ("own", "solo.py", "x = 1\n"),
        ("own", "a.yaml", with_python("a", "shared.py")), ("own", "b.yaml", with_python("b", "shared.py")),
        ("own", "shared.py", "y = 2\n"))
    version = lambda machine_id: service.get_machine(machine_id)["versions"][f"{machine_id}.yaml"]  # noqa: E731

    solo = service.delete_machine("solo", version("solo"))
    shared = service.delete_machine("a", version("a"))

    assert solo == {"deleted": "solo", "files": ["solo.yaml", "solo.py"], "kept_module": None}
    assert shared == {"deleted": "a", "files": ["a.yaml"], "kept_module": "shared.py"}
    assert sorted(p.name for p in own.iterdir()) == ["b.yaml", "shared.py"]


def test_delete_through_the_service_refuses_an_imported_a_shipped_and_a_changed_machine(tmp_path):
    from plugins.stategraph.service import ServiceError

    importer = MACHINE.format(id="host").replace("initial: a\n", "imports: {part: ./part.yaml}\ninitial: a\n")
    service, own, shipped = service_over(tmp_path, ("own", "part.yaml", MACHINE.format(id="part")),
                                         ("own", "host.yaml", importer),
                                         ("shipped", "example.yaml", MACHINE.format(id="example")))

    refused = {}
    for machine_id, seen in (("part", version_of(MACHINE.format(id="part"))),
                             ("example", version_of(MACHINE.format(id="example"))), ("host", "stale")):
        with pytest.raises(ServiceError) as caught:
            service.delete_machine(machine_id, seen)
        refused[machine_id] = (caught.value.status, caught.value.message)

    assert refused["part"][0] == 409 and "imported by host" in refused["part"][1]
    assert refused["example"][0] == 403 and refused["host"][0] == 409
    assert sorted(p.name for p in own.iterdir()) == ["host.yaml", "part.yaml"] and (shipped / "example.yaml").exists()


def test_a_new_module_whose_name_a_file_has_already_is_named_not_called_a_change(tmp_path):
    """The panel's module button writes <id>.py as new; an earlier module left there is not "the file changed"."""
    from plugins.stategraph.service import ServiceError

    service, own, _ = service_over(tmp_path, ("own", "m.yaml", MACHINE.format(id="m")), ("own", "m.py", "old = 1\n"))
    versions = service.get_machine("m")["versions"]

    with pytest.raises(ServiceError) as caught:
        service.save_machine("m", {"m.yaml": with_python("m", "m.py"), "m.py": "new = 2\n"}, versions)
    saved = service.save_machine("m", {"m.yaml": with_python("m", "m.py")}, versions)  # the panel's "use it"

    assert caught.value.status == 409 and caught.value.message.startswith("m.py exists already"), caught.value.message
    assert (own / "m.py").read_text(encoding="utf-8") == "old = 1\n"
    assert set(saved["versions"]) == {"m.yaml"} and "m.py" in service.get_machine("m")["files"]


@pytest.mark.parametrize("group, folder", [("Writer / v6", "Writer/v6"), ("/a//b/", "a/b"), ("", "")])
def test_a_machines_group_is_its_folder_path(group, folder):
    text = MACHINE.format(id="m") + f"group: {group!r}\n"
    tree = load_tree("/m/m.yaml", SnapshotSources({"/m/m.yaml": text}))

    assert not tree.problems, tree.problems
    assert tree.root_file.spec.group == folder
