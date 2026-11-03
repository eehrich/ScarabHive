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
