"""The shared result clip of the socket/subprocess plugins (blender, godot)."""
import json

from agent_system.utils.clip import clip_value, file_fingerprint, middle_cut


def _size(value):
    return len(value if isinstance(value, str) else json.dumps(value))


def test_small_values_pass_untouched():
    assert clip_value({"a": 1}, 100) == ({"a": 1}, "")


def test_a_long_string_inside_a_mapping_is_cut():
    """blender's copy left it whole and still said 'truncated'."""
    clipped, note = clip_value({"image_base64": "x" * 5000, "ok": True}, 1000)
    assert isinstance(clipped, dict) and clipped["ok"] is True
    assert _size(clipped) <= 1000 and "image_base64" in note


def test_the_longest_list_is_halved_and_counted():
    clipped, note = clip_value({"objects": list(range(2000))}, 500)
    assert _size(clipped) <= 500
    assert clipped["objects_omitted"] == 2000 - len(clipped["objects"])
    assert "objects" in note


def test_a_nested_tree_is_bounded():
    tree = {"root": {f"n{i}": {"name": f"node {i}"} for i in range(500)}}
    clipped, note = clip_value(tree, 1000)
    assert _size(clipped) <= 1000 + 200 and note


def test_middle_cut_keeps_both_ends():
    line = "start " + "x" * 300 + " the reason"
    cut = middle_cut(line, 60)
    assert len(cut) <= 60 and cut.startswith("start") and cut.endswith("the reason")


def test_fingerprint(tmp_path):
    assert file_fingerprint(tmp_path / "missing") is None
    (tmp_path / "f").write_text("abc")
    assert file_fingerprint(tmp_path / "f")[1] == 3


def test_many_small_fields_still_meet_the_limit():
    value = {f"field_{i}": "x" * 40 for i in range(1000)}

    clipped, note = clip_value(value, 1000)

    assert isinstance(clipped, dict) and clipped
    assert len(json.dumps(clipped)) <= 1000
    assert f"of {len(value)} fields omitted" in note


def test_a_string_of_escaped_characters_is_cut_not_dropped():
    clipped, note = clip_value({"stdout": "ü" * 80000, "exit_code": 0}, 60000)

    assert clipped["stdout"].startswith("ü") and clipped["exit_code"] == 0
    assert _size(clipped) <= 60000 and "omitted" not in note


def test_a_list_emptied_by_halving_stays_a_list():
    clipped, _ = clip_value({"items": [{"n": i} for i in range(4000)], **{f"f{i}": "x" * 60 for i in range(40)}}, 1000)

    assert clipped.get("items", []) == []


def test_scalars_stay_whatever_the_limit():
    """A caller reads scene["object_count"] on exactly the large scenes."""
    value = {"count": 3, "flag": True, "name": "x" * 40, "values": []}

    clipped, note = clip_value(value, 10)

    assert clipped["count"] == 3 and clipped["flag"] is True and clipped["values"] == []
    assert note

    only_scalars = {f"f{i}": i for i in range(100)}
    assert clip_value(only_scalars, 10) == (only_scalars, "oversized scalars kept as they are")


def test_escaped_characters_count_before_the_cut():
    clipped, note = clip_value({"stdout": "ü" * 14000, "exit_code": 0}, 60000)

    assert clipped["stdout"].startswith("ü") and "omitted" not in note


def test_a_serialised_tree_counts_its_escapes():
    value = {f"tree_{i}": {"k": {"q": '"' * 40000}} for i in range(3)}

    clipped, note = clip_value(value, 60000)

    assert set(clipped) == set(value) and "omitted" not in note
    assert _size(clipped) <= 60000
