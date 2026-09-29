"""What gets indexed is decided by CONTENT, never by extension.

The engine used to keep an allow-list of "text" extensions. Anything unlisted
was silently dropped from the index, and grep_search then answered "0 matches
in 0 files" -- which reads like "the string is not there" rather than "this
file was never indexed". Assembler sources (.asm/.s/.inc) were invisible that
way. No list can be complete, so there is none.
"""
import pytest

from plugins.file_ops.search import FileSearchEngine


@pytest.fixture
def engine(tmp_path):
    return FileSearchEngine([tmp_path], {"enable_indexing": True,
                                         "index_on_startup": False})


class TestTextDetection:
    @pytest.mark.parametrize("name", [
        "cube.asm", "boot.s", "hw.inc", "vectors.i",      # the reported case
        "main.py", "readme.md", "conf.yaml", "data.json",
        "app.vue", "q.sql", "Makefile", "Dockerfile",     # no extension at all
        "weird.xyzzy", "no_extension_at_all",             # never heard of
    ])
    def test_text_content_is_indexed_whatever_it_is_called(self, engine, tmp_path, name):
        path = tmp_path / name
        path.write_text("move.w d0,d1\n dbf d7,.loop\n rts\n", encoding="utf-8")
        assert engine._is_text_file(path) is True

    @pytest.mark.parametrize("payload", [
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",      # PNG header
        b"MZ\x90\x00\x03\x00\x00\x00\x04\x00",       # DOS/PE executable
        b"PK\x03\x04\x14\x00\x00\x00\x08\x00",       # zip
        b"\x7fELF\x02\x01\x01\x00",                  # ELF
        bytes(range(256)) * 4,                       # arbitrary binary
    ])
    def test_binary_content_is_skipped(self, engine, tmp_path, payload):
        path = tmp_path / "thing.dat"
        path.write_bytes(payload)
        assert engine._is_text_file(path) is False

    def test_a_png_named_like_source_is_still_skipped(self, engine, tmp_path):
        """The decision cannot be fooled by the name -- in either direction."""
        path = tmp_path / "actually_an_image.py"
        path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 200)
        assert engine._is_text_file(path) is False

    def test_utf8_umlauts_and_emoji_count_as_text(self, engine, tmp_path):
        path = tmp_path / "prosa.txt"
        path.write_text("Grüße, Straße, 日本語, ✻ und ↑↓", encoding="utf-8")
        assert engine._is_text_file(path) is True

    def test_latin1_bytes_count_as_text(self, engine, tmp_path):
        """Old sources are often not UTF-8; they must stay searchable."""
        path = tmp_path / "alt.asm"
        path.write_bytes("; Grüße vom Amiga\n rts\n".encode("latin-1"))
        assert engine._is_text_file(path) is True

    def test_empty_and_unreadable_files_are_skipped_without_raising(self, engine, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")
        assert engine._is_text_file(empty) is False
        assert engine._is_text_file(tmp_path / "does_not_exist.txt") is False


class TestSizeGuard:
    """A huge file must never reach the content sniff -- the size check runs
    first, so a 2 GB blob in the directory costs one stat(), not a read."""

    @pytest.mark.asyncio
    async def test_files_above_the_limit_are_not_indexed(self, tmp_path):
        engine = FileSearchEngine([tmp_path], {
            "enable_indexing": True, "index_on_startup": False,
            "max_file_size_for_indexing_kb": 1,       # 1 KB
        })
        (tmp_path / "small.asm").write_text("rts\n", encoding="utf-8")
        (tmp_path / "huge.asm").write_text("rts\n" + "x" * 5000, encoding="utf-8")

        result = await engine.grep_search(query="rts")
        hit_files = {m["file_path"] for m in result["matches"]}
        assert any("small.asm" in f for f in hit_files)
        assert not any("huge.asm" in f for f in hit_files)

    @pytest.mark.asyncio
    async def test_the_limit_is_configurable(self, tmp_path):
        (tmp_path / "medium.asm").write_text("rts\n" + "x" * 5000, encoding="utf-8")

        tight = FileSearchEngine([tmp_path], {
            "enable_indexing": True, "index_on_startup": False,
            "max_file_size_for_indexing_kb": 1})
        generous = FileSearchEngine([tmp_path], {
            "enable_indexing": True, "index_on_startup": False,
            "max_file_size_for_indexing_kb": 1024})

        assert (await tight.grep_search(query="rts"))["total_files"] == 0
        assert (await generous.grep_search(query="rts"))["total_files"] == 1


class TestGrepFindsWhatIsThere:
    @pytest.mark.asyncio
    async def test_assembler_source_is_searchable(self, engine, tmp_path):
        """The exact report: several 'dbf' and 'rts' in .asm files, 0 matches."""
        (tmp_path / "cube.asm").write_text(
            "Loop:\n    move.w  d0,d1\n    dbf     d7,Loop\n    rts\n",
            encoding="utf-8")
        (tmp_path / "tunnel.asm").write_text(
            "Draw:\n    dbf     d6,Draw\n    rts\n", encoding="utf-8")

        for query, files in (("dbf", 2), ("rts", 2)):
            result = await engine.grep_search(query=query)
            assert result["status"] == "success"
            assert result["total_matches"] >= files, f"{query}: {result}"
            assert result["total_files"] == files

    @pytest.mark.asyncio
    async def test_binary_content_never_shows_up_in_results(self, engine, tmp_path):
        (tmp_path / "code.asm").write_text("rts\n", encoding="utf-8")
        (tmp_path / "blob.bin").write_bytes(b"rts\x00" + b"\x00" * 500)

        result = await engine.grep_search(query="rts")
        hit_files = {m["file_path"] for m in result["matches"]}
        assert not any("blob.bin" in f for f in hit_files)
