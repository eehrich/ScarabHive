"""What loading and evaluating cost, and that the shortcuts change nothing but that.

A resume loads a large companion module and its YAML several times, and every template expression used to copy and hash
all of ctx. These tests hold the shortcuts to the behaviour of the way they bypass.
"""
from __future__ import annotations

import builtins
import gc
import linecache
import threading

import pytest

from plugins.stategraph.model import code as code_module
from plugins.stategraph.model.code import CodeError, Namespace, Scope, load_module, scan_module
from plugins.stategraph.model.loader import load_snapshot


def _guard_counted(monkeypatch) -> list:
    """Every hash of ctx the guard takes."""
    hashes = []
    real = code_module.fingerprint

    def counted(value):
        hashes.append(1)
        return real(value)
    monkeypatch.setattr(code_module, "fingerprint", counted)
    return hashes


def _scope() -> dict:
    return {"ctx": Scope({"notes": ["a", "b"], "n": 2, "deep": {"k": {"v": 1}}, "text": " Hi "}, label="ctx")}


# ---------------------------------------------------------------- the guard

@pytest.mark.parametrize("source, expected", [
    ("ctx.n", 2),
    ("ctx.n + 1", 3),
    ("len(ctx.notes)", 2),
    ("ctx.deep['k']['v']", 1),
    ("ctx.deep.get('k')", {"v": 1}),
    ("ctx.deep.get('none', 'x')", "x"),
    ("[n.upper() for n in ctx.notes]", ["A", "B"]),
    ("f'{ctx.n}x'", "2x"),
    ("ctx.text.strip().lower()", "hi"),
    ("'yes' if ctx.n > 1 else 'no'", "yes"),
    ("sorted(ctx.notes)[-1]", "b"),
    ("any(n == 'a' for n in ctx.notes)", True),
])
def test_code_that_only_reads_takes_no_copy_and_no_hash_of_ctx(monkeypatch, source, expected):
    hashes = _guard_counted(monkeypatch)
    assert Namespace().evaluate(source, _scope(), "w") == expected
    assert hashes == [], source


def test_an_answer_of_code_that_only_reads_is_not_ctx_itself():
    scope = _scope()
    notes = Namespace().evaluate("ctx.notes", scope, "w")
    notes.append("mine")
    deep = Namespace().evaluate("ctx.deep.get('k')", scope, "w")
    deep["v"] = 99
    assert scope["ctx"]._data["notes"] == ["a", "b"]
    assert scope["ctx"]._data["deep"]["k"]["v"] == 1


@pytest.mark.parametrize("source", [
    "ctx.notes.append('x')",
    "ctx.notes.clear()",
    "ctx.deep.update({'x': 1})",
    "ctx.deep.setdefault('x', 1)",
    "ctx.deep['k'].pop('v')",
    "ctx.notes.reverse()",
    "[ctx.notes.append(n) for n in ['x']]",
    "ctx.notes.__iadd__(['x'])",
])
def test_code_that_writes_is_refused_and_leaves_ctx_alone(source):
    scope = _scope()
    before = repr(scope["ctx"]._data)
    with pytest.raises(CodeError, match="mutated"):
        Namespace().evaluate(source, scope, "w")
    assert repr(scope["ctx"]._data) == before


def test_a_function_of_the_companion_module_that_writes_is_refused():
    def writes(ctx):
        ctx.notes.append("x")
        return 1
    with pytest.raises(CodeError, match="mutated"):
        Namespace({"writes": writes}).evaluate("writes(ctx)", _scope(), "w")


def test_a_companion_module_s_name_is_not_taken_for_the_builtin_or_the_method_it_looks_like():
    """`len` here writes, and so does `get` of an object of the module: the names prove nothing."""
    def len(value):  # noqa: A001
        value.append("x")
        return 0

    class Writes:
        def get(self, ctx):
            ctx.notes.append("x")
            return 0

    for module, source in (({"len": len}, "len(ctx.notes)"), ({"thing": Writes()}, "thing.get(ctx)")):
        scope = _scope()
        with pytest.raises(CodeError, match="mutated"):
            Namespace(module).evaluate(source, scope, "w")
        assert scope["ctx"]._data["notes"] == ["a", "b"]


def test_a_builtin_the_scope_has_rebound_takes_the_guarded_way(monkeypatch):
    hashes = _guard_counted(monkeypatch)
    scope = {**_scope(), "len": lambda value: value.append("x")}
    with pytest.raises(CodeError, match="mutated"):
        Namespace().evaluate("len(ctx.notes)", scope, "w")
    assert hashes, "fixture: the guard looked"
    assert scope["ctx"]._data["notes"] == ["a", "b"]


@pytest.mark.parametrize("source", [
    "sorted(['x'], key=ctx.notes.append)",
    "max(['x'], key=ctx.notes.append)",
    "[len('x') for len in [ctx.notes.append]]",
    "sorted(['x'], **{'key': ctx.notes.append})",
    "max(['x'], **{'key': ctx.notes.append})",
    "[0 for ctx.notes[0] in ['x']]",
    "[0 for ctx.deep['k'] in [1]]",
    "{0 for ctx.notes[:] in [[7]]}",
    "[0 for a, *ctx.deep['s'] in [[1, 2]]]",
    "[0 for sg.ctx.notes[0] in ['x']]",
    "any(0 for ctx.notes[0] in ['x'])",
])
def test_code_that_writes_through_something_it_is_given_is_refused(source):
    """The write stands nowhere as a call of a method: a key function, a rebound builtin, a comprehension's target."""
    scope = _scope()
    before = repr(scope["ctx"]._data)
    with pytest.raises(CodeError, match="mutated"):
        Namespace().evaluate(source, scope, "w")
    assert repr(scope["ctx"]._data) == before


@pytest.mark.parametrize("source", ["[ctx.notes.append]", "(ctx.notes.append,)", "{'a': ctx.deep.update}"])
def test_a_bound_method_in_an_answer_is_bound_to_a_copy(source):
    """deepcopy would keep it, and whoever calls it would write to ctx."""
    scope = _scope()
    value = Namespace().evaluate(source, scope, "w")
    method = next(iter(value.values())) if isinstance(value, dict) else value[0]
    assert method.__self__ is not scope["ctx"]._data["notes"]
    assert method.__self__ is not scope["ctx"]._data["deep"]


def test_an_answer_keeps_its_shape_on_the_short_way(monkeypatch):
    hashes = _guard_counted(monkeypatch)
    value = Namespace().evaluate("(ctx.notes, {'n': ctx.n, 'none': None}, [1.5, True])", _scope(), "w")
    assert value == (["a", "b"], {"n": 2, "none": None}, [1.5, True])
    assert type(value) is tuple
    assert hashes == []


@pytest.mark.parametrize("source, expected", [
    ("[ctx.deep.items()]", [[["k", {"v": 1}]]]),
    ("{'keys': ctx.deep.keys()}", {"keys": ["k"]}),
    ("((n for n in ctx.notes), 1)", [["a", "b"], 1]),
])
def test_an_answer_that_holds_what_does_not_copy_comes_from_the_guarded_way(source, expected):
    assert code_module.plain(Namespace().evaluate(source, _scope(), "w")) == expected


@pytest.mark.parametrize("source", ["[ctx]", "{'a': ctx}", "(ctx, 1)"])
def test_an_answer_that_holds_a_scope_is_no_crash(source):
    value = Namespace().evaluate(source, _scope(), "w")
    assert value, source


@pytest.mark.parametrize("source", ["ctx.notes.__len__()", "(lambda: 1)()", "(x := 1)"])
def test_what_the_list_does_not_know_takes_the_guarded_way(monkeypatch, source):
    hashes = _guard_counted(monkeypatch)
    try:
        Namespace().evaluate(source, _scope(), "w")
    except CodeError:
        pass
    assert hashes, source


@pytest.mark.parametrize("source, expected", [
    ("ctx", {"notes": ["a", "b"], "n": 2, "deep": {"k": {"v": 1}}, "text": " Hi "}),
    ("ctx.deep.items()", [["k", {"v": 1}]]),
    ("ctx.deep.keys()", ["k"]),
    ("(n for n in ctx.notes)", ["a", "b"]),
])
def test_a_lazy_answer_comes_out_of_the_guard_s_copy(source, expected):
    """A view or an iterator over ctx would read what ctx holds when it is read: over a copy, as ever."""
    scope = _scope()
    value = Namespace().evaluate(source, scope, "w")
    scope["ctx"]._data["deep"]["k"]["v"] = 99
    scope["ctx"]._data["deep"]["later"] = 1
    scope["ctx"]._data["notes"].append("later")
    assert code_module.plain(value) == expected, source


@pytest.mark.parametrize("source", ["ctx.nothing", "ctx.notes[9]", "ctx.deep['none']", "1 / 0", "len(ctx.n)"])
def test_code_that_raises_says_so_the_same_on_both_ways(source):
    with pytest.raises(CodeError) as reading:
        Namespace().evaluate(source, _scope(), "w")
    assert code_module._only_reads(source) is not None, "fixture: this one takes the short way"
    real = code_module._only_reads
    try:
        code_module._only_reads = lambda _source: None
        with pytest.raises(CodeError) as guarded:
            Namespace().evaluate(source, _scope(), "w")
    finally:
        code_module._only_reads = real
    assert str(reading.value) == str(guarded.value)


# ---------------------------------------------------------------- the companion module

def test_one_source_is_compiled_once_and_every_load_runs_it_anew(monkeypatch):
    source = "made = []\ndef add():\n    made.append(1)\n    return len(made)\n# one compile\n"
    compiles = []
    real = builtins.compile

    def counted(text, *args, **kwargs):
        if text == source:
            compiles.append(1)
        return real(text, *args, **kwargs)
    monkeypatch.setattr(builtins, "compile", counted)

    first, second = load_module(source, "m.py"), load_module(source, "m.py")
    assert len(compiles) == 1
    assert first.public["add"]() == 1
    assert second.public["add"]() == 1, "a module of its own: the other's state is not here"
    assert first.module["__name__"] != second.module["__name__"]


def test_a_changed_source_is_compiled_for_itself_and_shows_its_own_lines():
    first, second = load_module("def f():\n    return 'one'\n", "m.py"), load_module("def f():\n    return 'two'\n", "m.py")
    assert (first.public["f"](), second.public["f"]()) == ("one", "two")
    one, two = first.public["f"].__code__.co_filename, second.public["f"].__code__.co_filename
    assert linecache.getline(one, 2).strip() == "return 'one'"
    assert linecache.getline(two, 2).strip() == "return 'two'"


def test_the_lines_of_a_source_stay_until_the_last_module_running_it_is_gone():
    first = load_module("def f():\n    return 'shared'\n", "shared.py")
    second = load_module("def f():\n    return 'shared'\n", "shared.py")
    shown = first.public["f"].__code__.co_filename
    assert second.public["f"].__code__.co_filename == shown, "fixture: one source, one file name"

    del first
    gc.collect()
    assert linecache.getline(shown, 2).strip() == "return 'shared'", "the other module still runs it"

    del second
    gc.collect()
    assert shown not in linecache.cache


def test_the_garbage_collector_finishing_a_module_during_a_load_does_not_hang():
    """_forget runs from the collector, which may strike on the very thread that holds the lock."""
    def collected_in_a_load():
        with code_module._SOURCES_LOCK:
            code_module._forget("stategraph_machine_gone", "stategraph_source_gone/gone.py")

    thread = threading.Thread(target=collected_in_a_load, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "it waits for its own lock, and every load after it waits with it"


@pytest.mark.parametrize("load", [load_module, scan_module])
def test_a_source_that_does_not_compile_says_so_every_time(load):
    for _ in range(2):
        with pytest.raises(CodeError, match="does not compile"):
            load("def broken(:\n", "b.py")


def test_one_source_is_scanned_once_and_gives_the_same_names(monkeypatch):
    source = "def f():\n    pass\nVALUE = 1\n# one scan\n"
    scans = []
    real = code_module.module_names

    def counted(text):
        scans.append(1)
        return real(text)
    monkeypatch.setattr(code_module, "module_names", counted)

    first, second = scan_module(source, "s.py"), scan_module(source, "s.py")
    assert len(scans) == 1
    assert first.names() == second.names() >= {"f", "VALUE"}
    assert second.has_function("f")


# ---------------------------------------------------------------- the machine files

MACHINE = "stategraph: 1\nid: m\ninitial: a\nstates:\n  a:\n    type: final\n"


def test_loads_of_one_text_parse_once_and_stay_trees_of_their_own(monkeypatch):
    from plugins.stategraph.model import loader

    text = MACHINE + "# one parse\n"
    parses = []
    real = loader.parse_yaml

    def counted(value):
        parses.append(1)
        return real(value)
    monkeypatch.setattr(loader, "parse_yaml", counted)
    loader._parsed.cache_clear()

    first = load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
    second = load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
    assert first.ok and second.ok, first.problems
    assert len(parses) == 1
    assert first.root_file.spec is not second.root_file.spec
    first.add("error", "SG001", "only this tree's")
    assert second.ok


def test_a_file_that_does_not_parse_says_so_every_time():
    text = "stategraph: 1\nid: [unclosed\n"
    for _ in range(2):
        tree = load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
        assert not tree.ok
        assert any("YAML" in problem.message for problem in tree.problems)


def test_each_load_gets_a_plain_copy_of_its_own(monkeypatch):
    """A spec holds parts of what it was validated from, and the document's walks are shared between loads."""
    from plugins.stategraph.model import loader

    text = MACHINE + "# own data\n"
    seen = []
    real = loader.MachineSpec.model_validate

    def captured(data, *args, **kwargs):
        seen.append(data)
        return real(data, *args, **kwargs)
    monkeypatch.setattr(loader.MachineSpec, "model_validate", captured)

    load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
    seen[0]["states"]["a"]["type"] = "changed by the first tree"
    load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
    assert seen[1]["states"]["a"]["type"] == "final"


def test_loads_of_one_text_walk_the_document_once(monkeypatch):
    from plugins.stategraph.model import loader

    text = MACHINE + "# one walk\n"
    walks = []
    real = loader.yaml_bounds

    def counted(doc):
        walks.append(1)
        return real(doc)
    monkeypatch.setattr(loader, "yaml_bounds", counted)
    for _ in range(3):
        assert load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}}).ok
    assert len(walks) == 1


def test_a_too_big_or_badly_keyed_file_says_so_every_time():
    text = "stategraph: 1\nid: m\ninitial: a\ntrue: x\nstates:\n  a:\n    type: final\n"
    for _ in range(2):
        tree = load_snapshot({"root": "m.yaml", "files": {"m.yaml": text}})
        assert any("boolean" in problem.message for problem in tree.problems), tree.problems


def test_one_piece_of_code_is_analysed_once_for_its_names(monkeypatch):
    source = "ctx.loaded_once + known_name"
    parses = []
    real = code_module.ast.parse

    def counted(text, *args, **kwargs):
        if text == source:
            parses.append(1)
        return real(text, *args, **kwargs)
    monkeypatch.setattr(code_module.ast, "parse", counted)

    known = code_module.analyse(source, mode="eval", allowed={"known_name"})
    again = code_module.analyse(source, mode="eval", allowed=["known_name"])
    other = code_module.analyse(source, mode="eval", allowed=())
    assert known is again and known.unknown == ()
    assert other.unknown == ("known_name",), "other names in scope, another answer"
    assert len(parses) == 2



def test_a_schema_found_valid_is_not_checked_again_and_an_invalid_one_always_is(monkeypatch):
    from types import SimpleNamespace

    from plugins.stategraph.model import validate

    problems, checks = [], []
    real = validate.jsonschema.validators.validator_for

    def counted(schema, *args, **kwargs):
        if isinstance(schema, dict) and str(schema.get("title", "")).startswith("checked "):  # not its meta schemas
            checks.append(1)
        return real(schema, *args, **kwargs)
    monkeypatch.setattr(validate.jsonschema.validators, "validator_for", counted)
    context = SimpleNamespace(problem=lambda *args: problems.append(args))

    good = {"type": "object", "title": "checked once"}
    validate._check_schema(context, good, ["s"], "SG")
    validate._check_schema(context, dict(good), ["s"], "SG")
    assert len(checks) == 1 and not problems

    bad = {"type": "no such type", "title": "checked every time"}
    validate._check_schema(context, bad, ["s"], "SG")
    validate._check_schema(context, bad, ["s"], "SG")
    assert len(problems) == 2
    assert len(checks) == 3


def test_a_load_searches_the_machine_folders_once_however_many_files(tmp_path, monkeypatch):
    from plugins.stategraph.store import MachineStore

    (tmp_path / "root.yaml").write_text(
        "stategraph: 1\nid: root\ninitial: a\nimports: {one: ./one.yaml, two: ./two.yaml}\n"
        "states:\n  a:\n    type: final\n", encoding="utf-8")
    for name in ("one", "two"):
        (tmp_path / f"{name}.yaml").write_text(
            f"stategraph: 1\nid: {name}\ninitial: a\nstates:\n  a:\n    type: final\n", encoding="utf-8")
    store = MachineStore([str(tmp_path)], [])
    searches = []
    real = store.root_dirs

    def counted():
        searches.append(1)
        return real()
    monkeypatch.setattr(store, "root_dirs", counted)

    tree = store.load("root")
    assert tree.ok and len(tree.files) == 3, tree.problems
    assert len(searches) <= 2, "one to find the machine, one for all its files"


def test_a_load_still_reads_nothing_outside_the_machine_folders(tmp_path):
    from plugins.stategraph.store import MachineStore

    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    (outside / "secret.yaml").write_text("stategraph: 1\nid: secret\ninitial: a\nstates:\n  a: {type: final}\n",
                                         encoding="utf-8")
    (inside / "m.yaml").write_text("stategraph: 1\nid: m\ninitial: a\nimports: {s: ../outside/secret.yaml}\n"
                                   "states:\n  a:\n    type: final\n", encoding="utf-8")
    tree = MachineStore([str(inside)], []).load("m")
    assert not tree.ok
    assert any("outside the machine roots" in problem.message for problem in tree.problems), tree.problems


def test_a_part_an_answer_holds_twice_is_copied_once():
    """[[0] * 100] * 100 and deeper is small in memory, and would take seconds walked as a tree."""
    value = Namespace().evaluate("[[[[[0] * 100] * 100] * 100] * 100]", _scope(), "w")
    assert value[0][0] is value[0][1]
    assert value[0][0][0][0][0] == 0


def _schema_context(problems: list):
    from types import SimpleNamespace

    return SimpleNamespace(problem=lambda *args: problems.append(args))


def test_a_schema_that_is_no_plain_json_is_checked_every_time_and_never_taken_for_another():
    """A YAML date dumped by its repr reads like the string a valid schema holds: {pattern: 2020-01-01} is invalid."""
    import datetime

    from plugins.stategraph.model import validate

    problems = []
    context = _schema_context(problems)
    validate._check_schema(context, {"pattern": "datetime.date(2020, 1, 1)"}, ["s"], "SG")
    assert not problems, "fixture: a string is a pattern"
    for _ in range(2):
        validate._check_schema(context, {"pattern": datetime.date(2020, 1, 1)}, ["s"], "SG")
    assert len(problems) == 2


@pytest.mark.parametrize("address", [5, ["x"]])
def test_a_schema_with_a_nonsense_address_is_a_problem_and_no_crash(address):
    from plugins.stategraph.model import validate

    problems = []
    validate._check_schema(_schema_context(problems), {"$schema": address}, ["s"], "SG")
    assert len(problems) == 1 and "$schema" in problems[0][2]
