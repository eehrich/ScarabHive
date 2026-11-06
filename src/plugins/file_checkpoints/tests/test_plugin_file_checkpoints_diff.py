"""What a checkpoint changed in a file, as a diff -- through the real path: an agent
run writes through file_ops, the hooks record it, the rewinder's diff() reads it back.

What is pinned:

* the bytes after a change are found by their digest -- in the record (a later
  turn kept them as its "before") or on disk while the file there still holds
  them; once the file changed outside the record, no diff is shown instead of
  a wrong one;
* a created file diffs against nothing; a binary file is named, not diffed;
* a path or checkpoint the listing does not name gives None.
"""
from __future__ import annotations

import pytest

from file_rewind_rig import USER, SESSION, Rig, create, replace


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


async def _diff(rig: Rig, checkpoint: int, path) -> dict:
    return await rig.plugin.diff(user_id=USER, session_id=SESSION, messages=rig.messages(),
                                 checkpoint=checkpoint, path=str(path))


async def _two_turns(rig: Rig):
    work = rig.work
    (work / "a.txt").write_bytes(b"one\ntwo\n")
    await rig.turn("first", [replace(work / "a.txt", "one", "ONE")])
    await rig.turn("second", [replace(work / "a.txt", "two", "TWO"), create(work / "b.txt", "new\n")])
    numbers = [c["number"] for c in (await rig.checkpoints())["checkpoints"]]
    assert len(numbers) == 2, f"fixture: {numbers}"
    return numbers


async def test_each_checkpoint_shows_what_it_changed(rig):
    first, second = await _two_turns(rig)
    a = rig.work / "a.txt"

    early = await _diff(rig, first, a)   # its "after" is the bytes the second turn kept as its "before"
    late = await _diff(rig, second, a)   # its "after" is on disk
    created = await _diff(rig, second, rig.work / "b.txt")

    assert early["change"] == "modified" and early["note"] == ""
    assert "-one\n" in early["diff"] and "+ONE\n" in early["diff"] and "TWO" not in early["diff"]
    assert "-two\n" in late["diff"] and "+TWO\n" in late["diff"]
    assert created["change"] == "created" and "+new\n" in created["diff"]


async def test_a_file_changed_outside_since_shows_no_diff_rather_than_a_wrong_one(rig):
    _, second = await _two_turns(rig)
    a = rig.work / "a.txt"
    a.write_bytes(b"someone else\n")

    late = await _diff(rig, second, a)

    assert late["diff"] is None
    assert "not kept" in late["note"], late


async def test_a_binary_file_is_named_not_diffed(rig):
    work = rig.work
    (work / "img.bin").write_bytes(b"head\x00\x00tail")   # valid UTF-8: only the NULs tell it apart
    await rig.turn("bin", [create(work / "img.bin", "text now", overwrite=True)])
    number = (await rig.checkpoints())["checkpoints"][0]["number"]

    shown = await _diff(rig, number, work / "img.bin")

    assert shown["diff"] is None and shown["note"].startswith("Binary file"), shown


async def test_what_the_listing_does_not_name_is_none(rig):
    first, _ = await _two_turns(rig)

    assert await _diff(rig, first, rig.work / "b.txt") is None, "b.txt is the second checkpoint's"
    assert await _diff(rig, 99, rig.work / "a.txt") is None
    assert await rig.plugin.diff(user_id="someone_else", session_id=SESSION, messages=rig.messages(),
                                 checkpoint=first, path=str(rig.work / "a.txt")) is None


async def test_lines_end_only_at_a_newline(rig):
    """A form feed or U+2028 is part of a line, not the end of one: the numbers stay those of the file."""
    work = rig.work
    (work / "f.txt").write_bytes("page one\x0cstill one\u2028same line\nlast".encode("utf-8"))
    await rig.turn("edit", [replace(work / "f.txt", "last", "LAST")])
    number = (await rig.checkpoints())["checkpoints"][0]["number"]

    shown = await _diff(rig, number, work / "f.txt")

    assert "@@ -1,2 +1,2 @@" in shown["diff"], shown["diff"]
    assert shown["diff"].endswith("-last\n\\ No newline at end of file\n+LAST\n\\ No newline at end of file\n")
    assert shown["diff"].count("No newline") == 2, shown["diff"]


async def test_a_change_made_outside_the_agent_within_the_turn_is_named(rig):
    work = rig.work
    f = work / "f.txt"
    f.write_bytes(b"one\ntwo\n")
    await rig.turn("edit twice", [replace(f, "one", "ONE")], [replace(f, "two", "TWO")],
                   before_round={1: lambda: f.write_bytes(b"ONE\ntwo\nperson\n")})
    number = (await rig.checkpoints())["checkpoints"][0]["number"]

    shown = await _diff(rig, number, f)

    assert "+person\n" in shown["diff"], "fixture: the person's line is not in the turn's diff"
    assert "Not all of it is the agent's" in shown["note"], shown


async def test_an_after_too_big_to_read_says_so_not_that_it_changed(tmp_path):
    rig = await Rig(tmp_path).start(max_file_bytes=64)
    try:
        work = rig.work
        await rig.turn("big", [create(work / "big.txt", "z" * 200)])
        number = (await rig.checkpoints())["checkpoints"][0]["number"]

        shown = await _diff(rig, number, work / "big.txt")

        assert shown["diff"] is None and "larger than 64 bytes" in shown["note"], shown
    finally:
        await rig.close()


def test_a_binary_file_keeps_the_note_it_came_with():
    from plugins.file_checkpoints.diff import render

    shown = render(b"\x00", b"x", "Not all of it is the agent's: someone wrote.")

    assert shown["note"].startswith("Binary file") and "someone wrote" in shown["note"], shown
