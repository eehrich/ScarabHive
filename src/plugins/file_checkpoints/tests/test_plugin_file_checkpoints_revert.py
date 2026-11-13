"""Reverting one checkpoint, or one file of it: its changes undone, the checkpoints after it left as they are.

A file a later checkpoint changed again is refused like a file changed outside the agent -- the revert would take
that work with it -- and put back only with overwrite. What was reverted leaves the record; the rest of the
checkpoint, and its number, stay.
"""
from __future__ import annotations

import pytest

from agent_system.file_rewind import NOTHING, REFUSED, REWOUND, UNKNOWN_CHECKPOINT
from file_rewind_rig import Rig, create, replace, tree


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


async def _numbers(rig: Rig) -> list:
    return [(c["number"], sorted(f["path"] for f in c["files"])) for c in (await rig.checkpoints())["checkpoints"]]


async def test_one_checkpoint_is_reverted_and_the_later_ones_stay(rig):
    work = rig.work
    (work / "x.txt").write_text("v0")
    await rig.turn("one", [create(work / "a.txt", "A"), replace(work / "x.txt", "v0", "v1")])
    await rig.turn("two", [create(work / "b.txt", "B")])
    first, second = [n for n, _ in await _numbers(rig)]

    report = await rig.rewind(first, only=True)

    assert report["status"] == REWOUND, report["text"]
    assert tree(work) == {"x.txt": b"v0", "b.txt": b"B"}, "the later checkpoint was undone with it"
    assert report["text"].startswith(f"Reverted checkpoint {first}: 1 put back, 1 removed."), report["text"]
    assert await _numbers(rig) == [(second, [str(work / "b.txt")])], "the reverted changes stayed recorded"
    # The later checkpoint still puts back what it changed.
    assert (await rig.rewind(second))["status"] == REWOUND
    assert tree(work) == {"x.txt": b"v0"}


async def test_a_file_a_later_checkpoint_changed_again_is_refused_and_overwrite_takes_it_back(rig):
    work = rig.work
    (work / "x.txt").write_text("v0")
    await rig.turn("one", [replace(work / "x.txt", "v0", "v1")])
    await rig.turn("two", [replace(work / "x.txt", "v1", "v2")])
    first, second = [n for n, _ in await _numbers(rig)]

    refused = await rig.rewind(first, only=True)

    assert refused["status"] == REFUSED, refused["text"]
    assert refused["conflicts"] == [{"path": str(work / "x.txt"), "reason": f"checkpoint {second} changed it again since"}]
    assert refused["text"].startswith(f"Checkpoint {first} not reverted -- nothing was changed:"), refused["text"]
    assert (work / "x.txt").read_text() == "v2"

    report = await rig.rewind(first, only=True, overwrite=True)

    assert report["status"] == REWOUND, report["text"]
    assert (work / "x.txt").read_text() == "v0"
    # The later checkpoint's change of x.txt is no longer on disk: its row went with it, and with it its number.
    assert await _numbers(rig) == [], "the later checkpoint still claims a change the revert took back"


async def test_a_later_checkpoint_put_back_over_keeps_its_other_files_and_brings_nothing_back(rig):
    """Left as it was, the later row said x.txt went v1 -> v2, and a rewind of that checkpoint wrote v1: the very
    change the revert had just taken back."""
    work = rig.work
    (work / "x.txt").write_text("v0")
    await rig.turn("one", [replace(work / "x.txt", "v0", "v1")])
    await rig.turn("two", [replace(work / "x.txt", "v1", "v2"), create(work / "b.txt", "B")])
    first, second = [n for n, _ in await _numbers(rig)]

    assert (await rig.rewind(first, only=True, overwrite=True))["status"] == REWOUND

    assert await _numbers(rig) == [(second, [str(work / "b.txt")])]
    report = await rig.rewind(second)
    assert report["status"] == REWOUND, report["text"]
    assert tree(work) == {"x.txt": b"v0"}, "the rewind of the later checkpoint brought the reverted change back"


async def test_a_directory_a_later_checkpoint_filled_is_kept_and_says_by_whom(rig):
    work = rig.work
    await rig.turn("one", [create(work / "d" / "a.txt", "A")])
    await rig.turn("two", [create(work / "d" / "b.txt", "B")])
    first, second = [n for n, _ in await _numbers(rig)]

    report = await rig.rewind(first, only=True, overwrite=True)

    assert tree(work) == {"d": "<dir>", "d/b.txt": b"B"}, report["text"]
    reasons = [u["reason"] for u in report["unrestorable"]]
    assert all("did not create" not in r for r in reasons), reasons
    assert all(f"checkpoint {second} put files in it since" in r for r in reasons), reasons


async def test_one_file_is_reverted_and_the_rest_of_its_checkpoint_stays(rig):
    work = rig.work
    (work / "x.txt").write_text("v0")
    await rig.turn("one", [create(work / "a.txt", "A"), replace(work / "x.txt", "v0", "v1")])
    [(number, _)] = await _numbers(rig)

    report = await rig.rewind(number, path=str(work / "a.txt"))

    assert report["status"] == REWOUND, report["text"]
    assert tree(work) == {"x.txt": b"v1"}
    assert report["text"].startswith(f"Reverted {work / 'a.txt'} in checkpoint {number}: 0 put back, 1 removed."), \
        report["text"]
    assert await _numbers(rig) == [(number, [str(work / "x.txt")])], "the checkpoint lost its number or its other file"


async def test_a_file_recorded_under_two_spellings_is_reverted_as_the_one_file_it_is(rig):
    work = rig.work
    (work / "Probe.txt").write_text("")
    insensitive = (work / "probe.txt").exists()
    (work / "Probe.txt").unlink()
    if not insensitive:
        pytest.skip("the file system tells New.txt from new.txt")
    (work / "x.txt").write_text("v0")
    await rig.turn("create", [create(work / "New.txt", "A", overwrite=True),
                              create(work / "new.txt", "B", overwrite=True), replace(work / "x.txt", "v0", "v1")])
    [(number, files)] = await _numbers(rig)
    assert len(files) == 3, "fixture: both spellings are listed"

    report = await rig.rewind(number, path=str(work / "New.txt"))

    assert report["status"] == REWOUND, report["text"]
    assert not report["conflicts"], report["conflicts"]
    assert tree(work) == {"x.txt": b"v1"}
    assert await _numbers(rig) == [(number, [str(work / "x.txt")])], "the other spelling stayed listed"


async def test_what_names_nothing_to_revert_touches_nothing(rig):
    work = rig.work
    await rig.turn("one", [create(work / "a.txt", "A")])
    [(number, _)] = await _numbers(rig)

    assert (await rig.rewind(number, path=str(work / "other.txt")))["status"] == UNKNOWN_CHECKPOINT
    assert (await rig.rewind(None, only=True))["status"] == UNKNOWN_CHECKPOINT
    assert (await rig.rewind(number + 7, only=True))["status"] == UNKNOWN_CHECKPOINT
    assert tree(work) == {"a.txt": b"A"}

    (work / "a.txt").unlink()            # already as it was before the checkpoint
    nothing = await rig.rewind(number, only=True)
    assert nothing["status"] == NOTHING, nothing["text"]
    assert nothing["text"] == f"No file of checkpoint {number} needed putting back."
