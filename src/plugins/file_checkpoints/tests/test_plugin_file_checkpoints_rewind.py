"""file_checkpoints through the real path: a real agent run writes through file_ops, the
plugin's hooks record it, and a rewind puts the bytes back.

What is pinned:

* a rewind restores the exact bytes of every file the agent changed since the
  checkpoint -- binary content, CRLF line endings, modes -- deletes what it
  created (directories it created included) and brings back what it deleted
  or moved;
* a turn /undo dropped without its files stands behind the turn before it: a
  rewind further back undoes it too, a rewind of a LATER turn does not;
* numbered checkpoints rewind everything since, the conversation stays as it is;
* nothing the agent did not record is touched.
"""
from __future__ import annotations

import os
import stat

import pytest

from agent_system.file_rewind import NOTHING, REWOUND, UNKNOWN_CHECKPOINT
from file_rewind_rig import Rig, create, delete, move, rename, replace, tree


@pytest.fixture
async def rig(tmp_path):
    rig = await Rig(tmp_path).start()
    yield rig
    await rig.close()


BINARY = bytes(range(256)) + b"\x00\xff\xfe" * 100 + b"\x89PNG\r\n\x1a\n"


def _seed(rig: Rig) -> None:
    work = rig.work
    (work / "a.txt").write_bytes(b"line one\r\nline two\r\n")
    (work / "blob.bin").write_bytes(BINARY)
    (work / "tool.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(work / "tool.sh", 0o755)
    (work / "dir" / "inner").mkdir(parents=True)
    (work / "dir" / "x.txt").write_bytes(b"x")
    (work / "dir" / "inner" / "y.bin").write_bytes(BINARY[::-1])
    (work / "keep.txt").write_bytes(b"keep me")


class TestUndoFiles:

    async def test_the_last_turn_is_put_back_byte_for_byte(self, rig):
        _seed(rig)
        work = rig.work
        at_start = tree(work)
        await rig.turn("first", [replace(work / "a.txt", "line one", "LINE ONE"),
                                 create(work / "new.txt", "brand new")])
        after_first = tree(work)
        mode_before = stat.S_IMODE(os.stat(work / "tool.sh").st_mode)
        await rig.turn("second",
                       [delete(work / "blob.bin"), move(work / "dir", work / "moved" / "dir"),
                        create(work / "sub" / "deep" / "n.txt", "nested")],
                       [create(work / "a.txt", "replaced whole", overwrite=True),
                        create(work / "tool.sh", "#!/bin/sh\nexit 1\n", overwrite=True),
                        rename(work / "keep.txt", "kept.txt")])
        assert not (work / "blob.bin").exists() and (work / "moved" / "dir" / "inner").is_dir()

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == after_first, "the last turn was not put back exactly"
        assert stat.S_IMODE(os.stat(work / "tool.sh").st_mode) == mode_before
        assert not (work / "sub").exists(), "the directories the turn created stayed"
        assert not (work / "moved").exists()

        rig.undo_conversation()              # /undo: the second turn leaves the conversation
        report = await rig.rewind()          # ...and now the first one, with its files

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == at_start
        assert (work / "a.txt").read_bytes() == b"line one\r\nline two\r\n", "CRLF lost"

    async def test_a_turn_without_file_changes_rewinds_nothing(self, rig):
        _seed(rig)
        await rig.turn("just talk")

        report = await rig.rewind()

        assert report["status"] == NOTHING
        assert (rig.work / "keep.txt").read_bytes() == b"keep me"

    async def test_a_file_written_twice_in_a_turn_goes_back_to_before_the_first(self, rig):
        work = rig.work
        (work / "f.txt").write_text("v0")
        await rig.turn("edit twice", [replace(work / "f.txt", "v0", "v1")],
                       [replace(work / "f.txt", "v1", "v2")])

        report = await rig.rewind()

        assert report["status"] == REWOUND, report["text"]
        assert (work / "f.txt").read_text() == "v0"


class TestDroppedTurns:

    async def test_a_turn_dropped_without_its_files_is_undone_with_the_turn_before(self, rig):
        work = rig.work
        await rig.turn("one", [create(work / "a.txt", "A")])
        await rig.turn("two", [create(work / "b.txt", "B")])
        rig.undo_conversation()              # /undo without files: b.txt stays
        await rig.turn("two again", [create(work / "c.txt", "C")])

        report = await rig.rewind()          # the retried turn only

        assert report["status"] == REWOUND, report["text"]
        assert sorted(tree(work)) == ["a.txt", "b.txt"], (
            "a rewind of the retried turn undid the dropped attempt the person kept")

        rig.undo_conversation()
        report = await rig.rewind()          # turn one -- and the dropped turn after it

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {}

    async def test_the_listing_names_a_dropped_turn_between_its_neighbours(self, rig):
        work = rig.work
        await rig.turn("one", [create(work / "a.txt", "A")])
        await rig.turn("two", [create(work / "b.txt", "B")])
        rig.undo_conversation()
        await rig.turn("three", [create(work / "c.txt", "C")])

        listing = (await rig.checkpoints())["checkpoints"]

        assert [(c["turn"], c["dropped"], c["question"]) for c in listing] == [
            (1, False, "one"), (None, True, "two"), (2, False, "three")]
        assert listing[1]["after_turn"] == 1
        assert [f["path"] for f in listing[1]["files"]] == [str(work / "b.txt")]


class TestNumberedCheckpoints:

    async def test_a_checkpoint_rewinds_everything_since_and_keeps_the_conversation(self, rig):
        work = rig.work
        await rig.turn("one", [create(work / "a.txt", "A")])
        await rig.turn("two", [create(work / "b.txt", "B")])
        await rig.turn("three", [replace(work / "a.txt", "A", "AA")])
        before = rig.messages()

        report = await rig.rewind(checkpoint=2)

        assert report["status"] == REWOUND, report["text"]
        assert tree(work) == {"a.txt": b"A"}
        assert rig.messages() == before, "a files-only rewind changed the conversation"
        assert [c["question"] for c in (await rig.checkpoints())["checkpoints"]] == ["one"]

    async def test_an_unknown_number_touches_nothing(self, rig):
        await rig.turn("one", [create(rig.work / "a.txt", "A")])

        report = await rig.rewind(checkpoint=7)

        assert report["status"] == UNKNOWN_CHECKPOINT
        assert (rig.work / "a.txt").read_text() == "A"
