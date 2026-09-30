"""Tests for json_store plugin — validated JSON working documents."""

import json

import pytest
from unittest.mock import MagicMock

from plugins.json_store.server import JsonStoreServer
from agent_system.config.models import ToolServerConfig


@pytest.fixture
def mock_system_config():
    config = MagicMock()
    config.ssl_verify = True
    return config


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch):
    """Persistence is on by default and its storage_path is CWD-relative —
    run every test in a tmp CWD so no test ever writes into the repo's data/."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def server(mock_system_config):
    return JsonStoreServer(
        "json_store", mock_system_config, ToolServerConfig(type="json_store", enabled=True)
    )


SID = {"_session_id": "sess-1"}


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

class TestWrite:
    @pytest.mark.asyncio
    async def test_write_data_object(self, server):
        res = await server.write({**SID, "doc": "syn", "data": {"a": 1, "b": {"c": 2}}})
        assert res["status"] == "ok"
        assert res["top_level"] == ["a", "b"]
        assert res["replaced"] is False

    @pytest.mark.asyncio
    async def test_write_json_text(self, server):
        res = await server.write({**SID, "doc": "syn", "json_text": '{"x": "y"}'})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_write_strips_code_fence(self, server):
        res = await server.write(
            {**SID, "doc": "syn", "json_text": '```json\n{"x": 1}\n```'}
        )
        assert res["status"] == "ok"
        assert "stripped_code_fence" in res["repairs"]

    @pytest.mark.asyncio
    async def test_write_repairs_raw_newline_in_string(self, server):
        # The classic LLM mistake: real newline inside a string value.
        broken = '{"synopsis": "Zeile eins\nZeile zwei"}'
        res = await server.write({**SID, "doc": "syn", "json_text": broken})
        assert res["status"] == "ok"
        assert "auto_repaired_json" in res["repairs"]
        read = await server.read({**SID, "doc": "syn"})
        assert json.loads(read["json"])["synopsis"] == "Zeile eins\nZeile zwei"

    @pytest.mark.asyncio
    async def test_write_repairs_missing_comma(self, server):
        # json-repair recovers structural mistakes too (the forum #100943 case).
        res = await server.write({**SID, "doc": "syn", "json_text": '{"a": 1 "b": 2}'})
        assert res["status"] == "ok"
        assert "auto_repaired_json" in res["repairs"]
        assert json.loads((await server.read({**SID, "doc": "syn"}))["json"]) == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_write_repairs_truncated_json(self, server):
        # The real coordinator case: JSON cut off mid-string, no closing braces.
        truncated = '{"synopsis_text": "Die Story beginnt und dann bricht sie ab'
        res = await server.write({**SID, "doc": "syn", "json_text": truncated})
        assert res["status"] == "ok"
        assert "auto_repaired_json" in res["repairs"]
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data["synopsis_text"].startswith("Die Story beginnt")

    @pytest.mark.asyncio
    async def test_merge_also_repairs_invalid_json_text(self, server):
        await server.write({**SID, "doc": "syn", "data": {"genre": "X"}})
        # merge must run the same tolerant parse, not just write
        res = await server.merge(
            {**SID, "doc": "syn", "json_text": '{"synopsis_text": "a\nb", "extra": 1,}'})
        assert res["status"] == "ok"
        assert "auto_repaired_json" in res["repairs"]

    @pytest.mark.asyncio
    async def test_write_unrecoverable_input_precise_error(self, server):
        res = await server.write(
            {**SID, "doc": "syn", "json_text": "Hallo, das ist gar kein JSON."})
        assert res["status"] == "error"
        assert "Invalid JSON" in res["error"]

    @pytest.mark.asyncio
    async def test_write_requires_payload(self, server):
        # doc is optional now (auto-id), but a payload is still required.
        assert (await server.write({**SID, "doc": "x"}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_write_collision_errors_by_default(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": 1}})
        res = await server.write({**SID, "doc": "syn", "data": {"b": 2}})  # no if_exists
        assert res["status"] == "error" and "already exists" in res["error"]
        # original untouched
        assert json.loads((await server.read({**SID, "doc": "syn"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_write_if_exists_replace_overwrites(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": 1}})
        res = await server.write(
            {**SID, "doc": "syn", "data": {"b": 2}, "if_exists": "replace"})
        assert res["status"] == "ok" and res["replaced"] is True
        assert json.loads((await server.read({**SID, "doc": "syn"}))["json"]) == {"b": 2}

    @pytest.mark.asyncio
    async def test_write_fresh_doc_succeeds(self, server):
        res = await server.write({**SID, "doc": "fresh", "data": {"a": 1}})
        assert res["status"] == "ok" and res["replaced"] is False

    @pytest.mark.asyncio
    async def test_write_without_doc_mints_id(self, server):
        # A writer omits 'doc' and gets a fresh collision-free id back.
        res = await server.write({**SID, "data": {"a": 1}})
        assert res["status"] == "ok"
        gen = res["doc"]
        assert gen and gen.startswith("doc_")
        # The returned id is real and holds exactly what was written.
        read = await server.read({**SID, "doc": gen})
        assert json.loads(read["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_write_without_doc_ids_are_unique(self, server):
        # Parallel writers each get their own document — no clobbering.
        r1 = await server.write({**SID, "data": {"w": 1}})
        r2 = await server.write({**SID, "data": {"w": 2}})
        assert r1["doc"] != r2["doc"]
        listing = await server.list_docs(SID)
        assert listing["count"] == 2

    @pytest.mark.asyncio
    async def test_write_without_doc_ignores_if_exists(self, server):
        # if_exists has no meaning for an auto-id (always a fresh create).
        res = await server.write({**SID, "data": {"a": 1}, "if_exists": "error"})
        assert res["status"] == "ok"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

class TestRead:
    @pytest.mark.asyncio
    async def test_read_whole_canonical(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": {"b": [1, 2, 3]}}})
        res = await server.read({**SID, "doc": "syn"})
        assert res["status"] == "ok"
        assert json.loads(res["json"]) == {"a": {"b": [1, 2, 3]}}

    @pytest.mark.asyncio
    async def test_read_path_and_array_index(self, server):
        await server.write(
            {**SID, "doc": "syn",
             "data": {"milestones": [{"label": "M1"}, {"label": "M2"}]}}
        )
        res = await server.read({**SID, "doc": "syn", "path": "milestones[1].label"})
        assert json.loads(res["json"]) == "M2"

    @pytest.mark.asyncio
    async def test_read_missing_doc_lists_existing(self, server):
        await server.write({**SID, "doc": "other", "data": {}})
        res = await server.read({**SID, "doc": "nope"})
        assert res["status"] == "error"
        assert "other" in res["error"]

    @pytest.mark.asyncio
    async def test_read_bad_path_shows_keys(self, server):
        await server.write({**SID, "doc": "syn", "data": {"real_key": 1}})
        res = await server.read({**SID, "doc": "syn", "path": "wrong_key"})
        assert res["status"] == "error"
        assert "real_key" in res["error"]


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

class TestMerge:
    @pytest.mark.asyncio
    async def test_deep_merge_preserves_siblings(self, server):
        await server.write(
            {**SID, "doc": "syn",
             "data": {"synopsis": "alt", "chars": {"Nora": {"age": 34}}}}
        )
        res = await server.merge(
            {**SID, "doc": "syn",
             "data": {"synopsis": "neu", "chars": {"Erik": {"age": 42}}}}
        )
        assert res["status"] == "ok"
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data["synopsis"] == "neu"                    # scalar overwritten
        assert data["chars"]["Nora"]["age"] == 34           # sibling preserved
        assert data["chars"]["Erik"]["age"] == 42           # new nested key added

    @pytest.mark.asyncio
    async def test_merge_creates_missing_doc(self, server):
        res = await server.merge({**SID, "doc": "fresh", "data": {"a": 1}})
        assert res["status"] == "ok" and res["created"] is True

    @pytest.mark.asyncio
    async def test_merge_array_replace_and_concat(self, server):
        await server.write({**SID, "doc": "syn", "data": {"tags": ["a", "b"]}})
        await server.merge({**SID, "doc": "syn", "data": {"tags": ["c"]}})
        assert json.loads((await server.read({**SID, "doc": "syn"}))["json"])["tags"] == ["c"]
        await server.merge(
            {**SID, "doc": "syn", "data": {"tags": ["d"]}, "array_mode": "concat"}
        )
        assert json.loads(
            (await server.read({**SID, "doc": "syn"}))["json"]
        )["tags"] == ["c", "d"]

    @pytest.mark.asyncio
    async def test_merge_type_conflict_rejected_both_directions(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": 1}})
        res = await server.merge({**SID, "doc": "syn", "data": [1, 2]})
        assert res["status"] == "error"
        # reverse: dict into array doc must not silently discard the array
        await server.write({**SID, "doc": "arr", "data": [1, 2, 3]})
        res = await server.merge({**SID, "doc": "arr", "data": {"a": 1}})
        assert res["status"] == "error"
        assert json.loads((await server.read({**SID, "doc": "arr"}))["json"]) == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_merge_and_set_value_enforce_max_docs(self, mock_system_config):
        small = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={"max_docs": 1}),
        )
        await small.write({**SID, "doc": "one", "data": {"a": 1}})
        res = await small.merge({**SID, "doc": "two", "data": {"b": 2}})
        assert res["status"] == "error" and "Too many" in res["error"]
        res = await small.set_value({**SID, "doc": "three", "path": "x", "value": 1})
        assert res["status"] == "error" and "Too many" in res["error"]
        # existing doc still mutable at the cap
        assert (await small.merge({**SID, "doc": "one", "data": {"c": 3}}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_merge_reports_merged_keys(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": 1}})
        res = await server.merge({**SID, "doc": "syn", "data": {"b": 2, "c": 3}})
        assert res["merged_keys"] == ["b", "c"]


class TestMergeDoc:
    @pytest.mark.asyncio
    async def test_merge_doc_into_target(self, server):
        # writer produced a delta doc; moderator merges it into the main synopsis
        await server.write({**SID, "doc": "synopsis",
                            "data": {"genre": "SciFi", "chars": {"Nora": {"age": 34}}}})
        await server.write({**SID, "doc": "writer_delta",
                            "data": {"genre": "YA-SciFi", "chars": {"Erik": {"age": 42}}}})
        res = await server.manage_json(
            {**SID, "operation": "merge_doc", "source": "writer_delta", "doc": "synopsis"})
        assert res["status"] == "ok" and res["merged_from"] == "writer_delta"
        data = json.loads((await server.read({**SID, "doc": "synopsis"}))["json"])
        assert data["genre"] == "YA-SciFi"            # scalar overwritten
        assert data["chars"]["Nora"]["age"] == 34     # sibling preserved
        assert data["chars"]["Erik"]["age"] == 42     # delta added

    @pytest.mark.asyncio
    async def test_merge_doc_decouples_references(self, server):
        # after merge_doc the two stored docs must not alias (deepcopy'd source)
        await server.write({**SID, "doc": "tgt", "data": {"x": 1}})
        await server.write({**SID, "doc": "src", "data": {"list": [1, 2]}})
        await server.manage_json(
            {**SID, "operation": "merge_doc", "source": "src", "doc": "tgt"})
        # mutate the target's merged-in array; source must be unaffected
        await server.set_value({**SID, "doc": "tgt", "path": "list[0]", "value": 99})
        src = json.loads((await server.read({**SID, "doc": "src"}))["json"])
        assert src["list"] == [1, 2]

    @pytest.mark.asyncio
    async def test_merge_doc_missing_source_and_self(self, server):
        await server.write({**SID, "doc": "tgt", "data": {"a": 1}})
        r = await server.manage_json(
            {**SID, "operation": "merge_doc", "source": "ghost", "doc": "tgt"})
        assert r["status"] == "error" and "not found" in r["error"]
        r = await server.manage_json(
            {**SID, "operation": "merge_doc", "source": "tgt", "doc": "tgt"})
        assert r["status"] == "error" and "differ" in r["error"]

    @pytest.mark.asyncio
    async def test_merge_doc_creates_target(self, server):
        await server.write({**SID, "doc": "src", "data": {"a": 1}})
        res = await server.manage_json(
            {**SID, "operation": "merge_doc", "source": "src", "doc": "fresh"})
        assert res["status"] == "ok" and res["created"] is True
        assert json.loads((await server.read({**SID, "doc": "fresh"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_merge_doc_respects_key_model(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"key_models": {"synopsis": {"genre": {}}}}))
        await srv.write({**SID, "doc": "synopsis", "data": {"genre": "X"}})
        await srv.write({**SID, "doc": "delta", "data": {"erfunden": 1}})
        res = await srv.manage_json(
            {**SID, "operation": "merge_doc", "source": "delta", "doc": "synopsis"})
        assert res["status"] == "error"  # target's key model rejects the bad key
        # target unchanged
        assert json.loads((await srv.read({**SID, "doc": "synopsis"}))["json"]) == {"genre": "X"}


# ---------------------------------------------------------------------------
# set_value / delete
# ---------------------------------------------------------------------------

class TestSetDelete:
    @pytest.mark.asyncio
    async def test_set_value_index_on_an_object_is_refused(self, server):
        # 'a[0].b' on an object used to store the int key 0: unreadable by
        # any path, and "0" after a restart.
        await server.write({**SID, "doc": "syn", "data": {"a": {"x": 1}}})
        res = await server.set_value(
            {**SID, "doc": "syn", "path": "a[0].b", "value": 2})
        assert res["status"] == "error" and "array" in res["error"], res
        assert json.loads((await server.read({**SID, "doc": "syn"}))["json"]) == {
            "a": {"x": 1}}

    @pytest.mark.asyncio
    async def test_set_value_creates_intermediates(self, server):
        await server.write({**SID, "doc": "syn", "data": {}})
        res = await server.set_value(
            {**SID, "doc": "syn", "path": "chars.Nora.age", "value": 34}
        )
        assert res["status"] == "ok"
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data["chars"]["Nora"]["age"] == 34

    @pytest.mark.asyncio
    async def test_set_value_creates_arrays_for_index_segments(self, server):
        await server.write({**SID, "doc": "syn", "data": {}})
        res = await server.set_value(
            {**SID, "doc": "syn", "path": "items[0].label", "value": "M1"})
        assert res["status"] == "ok"
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data["items"] == [{"label": "M1"}]  # a real array, not {"0": ...}

    @pytest.mark.asyncio
    async def test_leading_array_index_path(self, server):
        await server.write({**SID, "doc": "arr", "data": [{"label": "M1"}]})
        res = await server.read({**SID, "doc": "arr", "path": "[0].label"})
        assert json.loads(res["json"]) == "M1"

    @pytest.mark.asyncio
    async def test_set_value_array_index_and_append(self, server):
        await server.write({**SID, "doc": "syn", "data": {"tags": ["a", "b"]}})
        await server.set_value({**SID, "doc": "syn", "path": "tags[1]", "value": "B"})
        await server.set_value({**SID, "doc": "syn", "path": "tags[2]", "value": "c"})
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data["tags"] == ["a", "B", "c"]
        res = await server.set_value({**SID, "doc": "syn", "path": "tags[9]", "value": "x"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_delete_keys_nested_and_missing(self, server):
        await server.write(
            {**SID, "doc": "syn", "data": {"keep": 1, "drop": 2, "nest": {"x": 1, "y": 2}}}
        )
        res = await server.delete_keys(
            {**SID, "doc": "syn", "paths": ["drop", "nest.y", "ghost"]}
        )
        assert res["deleted"] == ["drop", "nest.y"]
        assert res["missing"] == ["ghost"]
        data = json.loads((await server.read({**SID, "doc": "syn"}))["json"])
        assert data == {"keep": 1, "nest": {"x": 1}}

    @pytest.mark.asyncio
    async def test_delete_doc_and_list(self, server):
        await server.write({**SID, "doc": "a", "data": {"x": 1}})
        await server.write({**SID, "doc": "b", "data": {"y": 2}})
        res = await server.list_docs({**SID})
        assert res["count"] == 2
        res = await server.delete_doc({**SID, "doc": "a"})
        assert res["remaining"] == ["b"]


# ---------------------------------------------------------------------------
# outline / isolation / limits
# ---------------------------------------------------------------------------

class TestMisc:
    @pytest.mark.asyncio
    async def test_outline_no_values(self, server):
        await server.write(
            {**SID, "doc": "syn",
             "data": {"synopsis": "ein langer text", "milestones": [{"label": "M1"}]}}
        )
        res = await server.outline({**SID, "doc": "syn"})
        text = json.dumps(res["outline"])
        assert "string(15)" in text
        assert "ein langer text" not in text  # values never leak

    @pytest.mark.asyncio
    async def test_outline_depth_zero_respected(self, server):
        await server.write({**SID, "doc": "syn", "data": {"a": {"b": {"c": 1}}}})
        res = await server.outline({**SID, "doc": "syn", "depth": 0})
        assert res["outline"] == "object(1 keys)"  # not silently depth=3

    @pytest.mark.asyncio
    async def test_list_docs_top_level_consistent_for_arrays(self, server):
        await server.write({**SID, "doc": "arr", "data": [1, 2, 3]})
        res = await server.list_docs({**SID})
        assert res["docs"][0]["top_level"] == "array[3]"  # same shape as write summary

    @pytest.mark.asyncio
    async def test_session_isolation(self, server):
        await server.write({"_session_id": "s1", "doc": "syn", "data": {"a": 1}})
        res = await server.read({"_session_id": "s2", "doc": "syn"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_explicit_namespace_shared_across_sessions(self, server):
        # coordinator (session c) writes to a shared namespace...
        await server.write({"_session_id": "c", "namespace": "run-7",
                            "doc": "syn", "data": {"a": 1}})
        # ...a sub-agent in a DIFFERENT session reads it via the same namespace
        res = await server.read({"_session_id": "sub", "namespace": "run-7", "doc": "syn"})
        assert res["status"] == "ok"
        assert json.loads(res["json"]) == {"a": 1}
        # without the namespace, the sub-agent's own session sees nothing
        assert (await server.read({"_session_id": "sub", "doc": "syn"}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_namespace_ttl_eviction(self, mock_system_config):
        import time as _time
        # persist off → eviction is final (the pure memory-bound semantics;
        # with persistence the doc would reload from disk, see TestPersistence)
        server = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={"persist": False}))
        await server.write({"_session_id": "old", "doc": "syn", "data": {"a": 1}})
        # backdate the old namespace beyond the TTL, then touch another namespace
        server._ns_last_access["old"] = _time.time() - server._namespace_ttl_s - 1
        await server.list_docs({"_session_id": "fresh"})
        assert "old" not in server._docs  # evicted
        res = await server.read({"_session_id": "old", "doc": "syn"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_max_doc_bytes(self, mock_system_config):
        small = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={"max_doc_bytes": 50}),
        )
        res = await small.write({**SID, "doc": "big", "data": {"t": "x" * 100}})
        assert res["status"] == "error"
        assert "too large" in res["error"].lower()


# ---------------------------------------------------------------------------
# manage_json dispatcher (the actual exposed tool)
# ---------------------------------------------------------------------------

class TestManageJsonDispatch:
    @pytest.mark.asyncio
    async def test_read_over_the_limit_is_refused_not_cut(self, mock_system_config):
        srv = JsonStoreServer("json_store", mock_system_config, ToolServerConfig(
            type="json_store", enabled=True, config={"max_read_chars": 100}))
        await srv.write({**SID, "doc": "d", "data": {"big": "x" * 200, "small": 1}})
        res = await srv.manage_json({**SID, "operation": "read", "doc": "d"})
        assert res["status"] == "error" and "over the read limit of 100" in res["error"], res
        assert "json" not in res
        part = await srv.manage_json({**SID, "operation": "read", "doc": "d", "path": "small"})
        assert json.loads(part["json"]) == 1
        for bad in ("lots", 0, -5):
            srv = JsonStoreServer("json_store", mock_system_config, ToolServerConfig(
                type="json_store", enabled=True, config={"max_read_chars": bad}))
            assert srv._max_read_chars == 50000

    @pytest.mark.asyncio
    async def test_read_cap_follows_an_explicit_max_doc_bytes(self, mock_system_config):
        # A store sized for its documents (max_doc_bytes set, no max_read_chars)
        # must read a full-size document whole, indented.
        def make(**cfg):
            return JsonStoreServer("json_store", mock_system_config, ToolServerConfig(
                type="json_store", enabled=True, config=cfg))
        srv = make(max_doc_bytes=60000)
        assert srv._max_read_chars == 120000
        await srv.write({**SID, "doc": "d", "data": {"k": ["x" * 20] * 2000}})
        res = await srv.manage_json({**SID, "operation": "read", "doc": "d"})
        assert res["status"] == "ok" and res["chars"] > 50000, res.get("error")
        assert make(max_doc_bytes=60000, max_read_chars=1000)._max_read_chars == 1000
        assert make(max_doc_bytes=60000, max_read_chars="lots")._max_read_chars == 120000
        assert make()._max_read_chars == 50000

    @pytest.mark.asyncio
    async def test_outline_and_top_level_are_bounded(self, mock_system_config):
        srv = JsonStoreServer("json_store", mock_system_config, ToolServerConfig(
            type="json_store", enabled=True, config={"max_read_chars": 500}))
        res = await srv.manage_json({**SID, "operation": "write", "doc": "d",
                                     "data": {f"key{i:03d}": {"x": 1} for i in range(40)}})
        assert res["top_level"][-1] == "+25 more" and len(res["top_level"]) == 16, res
        listed = (await srv.manage_json({**SID, "operation": "list"}))["docs"][0]
        assert listed["top_level"][-1] == "+25 more"
        res = await srv.manage_json({**SID, "operation": "outline", "doc": "d"})
        assert res["status"] == "error" and "smaller 'depth'" in res["error"], res
        res = await srv.manage_json({**SID, "operation": "outline", "doc": "d", "depth": 0})
        assert res["outline"] == "object(40 keys)"

    def test_namespace_text_follows_session_scoped(self, mock_system_config):
        for scoped, text in ((True, "else your session"), (False, "else the default store")):
            srv = JsonStoreServer("json_store", mock_system_config, ToolServerConfig(
                type="json_store", enabled=True, config={"session_scoped": scoped}))
            desc = json.dumps(srv.get_tools(), ensure_ascii=False)
            assert text in desc and "{{" not in desc, desc

    @pytest.mark.asyncio
    async def test_wrong_parameter_type_answers_an_error(self, server):
        status = RecordingStatus()
        await server.manage_json(
            {**SID, "operation": "write", "doc": "d", "data": {"a": 1}})
        for call in ({"operation": "outline", "doc": "d", "depth": "abc"},
                     {"operation": "read", "doc": 5}):
            res = await server.manage_json({**SID, **call, "_status": status})
            assert res["status"] == "error", res
            assert res["error"].startswith("Invalid parameter for"), res
        assert len(status.errors) == 2 and not status.ended, status.errors

    @pytest.mark.asyncio
    async def test_dispatch_write_read_list(self, server):
        res = await server.manage_json(
            {**SID, "operation": "write", "doc": "d", "data": {"a": 1}})
        assert res["status"] == "ok"
        res = await server.manage_json({**SID, "operation": "read", "doc": "d"})
        assert json.loads(res["json"]) == {"a": 1}
        res = await server.manage_json({**SID, "operation": "list"})
        assert res["count"] == 1

    @pytest.mark.asyncio
    async def test_dispatch_merge_and_outline(self, server):
        await server.manage_json(
            {**SID, "operation": "write", "doc": "d", "data": {"a": {"x": 1}}})
        res = await server.manage_json(
            {**SID, "operation": "merge", "doc": "d", "data": {"a": {"y": 2}}})
        assert res["status"] == "ok"
        res = await server.manage_json({**SID, "operation": "outline", "doc": "d"})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_operation_lists_valid(self, server):
        res = await server.manage_json({**SID, "operation": "frobnicate"})
        assert res["status"] == "error"
        assert "merge" in res["error"]

    @pytest.mark.asyncio
    async def test_missing_operation(self, server):
        res = await server.manage_json({**SID, "doc": "d"})
        assert res["status"] == "error"


# ---------------------------------------------------------------------------
# key model (allowed keys per document)
# ---------------------------------------------------------------------------

@pytest.fixture
def modeled_server(mock_system_config):
    """Server with a key model for doc 'synopsis' (incl. '*' wildcard)."""
    return JsonStoreServer(
        "json_store", mock_system_config,
        ToolServerConfig(type="json_store", enabled=True, config={
            "key_models": {
                "synopsis": {
                    "synopsis": {},
                    "genre": {},
                    "key_characters": {
                        "*": {"age": {}, "role": {}, "arc": {}},
                    },
                },
            },
        }),
    )


class TestKeyModel:
    @pytest.mark.asyncio
    async def test_valid_keys_accepted(self, modeled_server):
        res = await modeled_server.write(
            {**SID, "doc": "synopsis",
             "data": {"synopsis": "text", "genre": "SciFi",
                      "key_characters": {"Nora": {"age": 34, "role": "Protagonistin"}}}}
        )
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_top_key_rejected_with_allowed_list(self, modeled_server):
        res = await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"synopsis": "x", "erfunden": 1}}
        )
        assert res["status"] == "error"
        assert "erfunden" in res["error"]
        assert "synopsis" in res["error"]  # allowed keys are listed

    @pytest.mark.asyncio
    async def test_wildcard_allows_dynamic_names_but_checks_children(self, modeled_server):
        # any character NAME is fine ('*'), but its fields are constrained
        res = await modeled_server.write(
            {**SID, "doc": "synopsis",
             "data": {"key_characters": {"Erik": {"age": 42, "hobby": "Angeln"}}}}
        )
        assert res["status"] == "error"
        assert "hobby" in res["error"]

    @pytest.mark.asyncio
    async def test_merge_violation_leaves_doc_unchanged(self, modeled_server):
        await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"synopsis": "original"}}
        )
        res = await modeled_server.merge(
            {**SID, "doc": "synopsis", "data": {"synopsis": "neu", "bogus": 1}}
        )
        assert res["status"] == "error"
        data = json.loads((await modeled_server.read({**SID, "doc": "synopsis"}))["json"])
        assert data == {"synopsis": "original"}  # rollback — nothing applied

    @pytest.mark.asyncio
    async def test_set_value_violation_rejected(self, modeled_server):
        await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"synopsis": "x"}}
        )
        res = await modeled_server.set_value(
            {**SID, "doc": "synopsis", "path": "invented_key", "value": 1}
        )
        assert res["status"] == "error"
        assert "invented_key" in res["error"]

    @pytest.mark.asyncio
    async def test_unmodeled_docs_stay_free(self, modeled_server):
        res = await modeled_server.write(
            {**SID, "doc": "scratch", "data": {"anything": {"goes": True}}}
        )
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_fuzzy_typo_key_auto_remapped(self, modeled_server):
        # 'genere' is a typo of 'genre' → auto-remapped, not rejected
        res = await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"genere": "SciFi"}})
        assert res["status"] == "ok"
        assert any("genere → genre" in r for r in res["remapped"])
        data = json.loads((await modeled_server.read({**SID, "doc": "synopsis"}))["json"])
        assert data == {"genre": "SciFi"}

    @pytest.mark.asyncio
    async def test_near_perfect_typo_auto_remapped(self, modeled_server):
        # 'key_charcters' (missing 'a') is close enough → auto-remapped
        res = await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"key_charcters": {"Nora": {"age": 3}}}})
        assert res["status"] == "ok"
        assert any("key_charcters → key_characters" in r for r in res["remapped"])

    @pytest.mark.asyncio
    async def test_collision_rejected_with_suggestion(self, modeled_server):
        # 'genere' would remap to 'genre', but 'genre' is already present → reject
        res = await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"genre": "A", "genere": "B"}})
        assert res["status"] == "error"
        assert "did you mean 'genre'" in res["error"]

    @pytest.mark.asyncio
    async def test_truly_unknown_key_lists_allowed(self, modeled_server):
        res = await modeled_server.write(
            {**SID, "doc": "synopsis", "data": {"völlig_erfunden_xyz": 1}})
        assert res["status"] == "error"
        assert "allowed:" in res["error"]  # no close match → allowed list


@pytest.fixture
def typed_server(mock_system_config):
    """Server whose key model enforces leaf TYPES (string node = type name)."""
    return JsonStoreServer(
        "json_store", mock_system_config,
        ToolServerConfig(type="json_store", enabled=True, config={
            "key_models": {
                "synopsis": {
                    "title_suggestion": "string",
                    "themes": "array",
                    "genre": {},
                    "milestones": {"label": "string", "setting": "string"},
                    "key_characters": {"*": {"age": {}, "role": "string"}},
                },
            },
        }),
    )


class TestKeyModelTypes:
    @pytest.mark.asyncio
    async def test_correct_leaf_types_accepted(self, typed_server):
        res = await typed_server.write(
            {**SID, "doc": "synopsis",
             "data": {"title_suggestion": "Der Fund", "themes": ["Mut", "Heimat"]}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_array_where_string_expected_rejected(self, typed_server):
        # the exact production bug: the writer put a list into title_suggestion.
        res = await typed_server.write(
            {**SID, "doc": "synopsis",
             "data": {"title_suggestion": ["Titel A", "Titel B"]}})
        assert res["status"] == "error"
        assert "title_suggestion" in res["error"]
        assert "must be string" in res["error"] and "array" in res["error"]

    @pytest.mark.asyncio
    async def test_string_where_array_expected_rejected(self, typed_server):
        res = await typed_server.write(
            {**SID, "doc": "synopsis", "data": {"themes": "Mut"}})
        assert res["status"] == "error"
        assert "themes" in res["error"] and "must be array" in res["error"]

    @pytest.mark.asyncio
    async def test_type_enforced_inside_array_elements(self, typed_server):
        # milestones is an array; each element's label must be a string.
        res = await typed_server.write(
            {**SID, "doc": "synopsis",
             "data": {"milestones": [{"label": ["nope"], "setting": "Wald"}]}})
        assert res["status"] == "error"
        assert "label" in res["error"] and "must be string" in res["error"]

    @pytest.mark.asyncio
    async def test_type_enforced_under_wildcard(self, typed_server):
        res = await typed_server.write(
            {**SID, "doc": "synopsis",
             "data": {"key_characters": {"Nora": {"age": 30, "role": ["Heldin"]}}}})
        assert res["status"] == "error"
        assert "role" in res["error"] and "must be string" in res["error"]

    @pytest.mark.asyncio
    async def test_free_node_still_accepts_any_type(self, typed_server):
        # genre is {} → any type allowed.
        res = await typed_server.write(
            {**SID, "doc": "synopsis", "data": {"genre": ["A", "B"]}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_null_passes_typed_leaf(self, typed_server):
        # a typed field left empty as null must NOT block the write (the guard is
        # against the wrong container, not against an empty/optional field).
        res = await typed_server.write(
            {**SID, "doc": "synopsis",
             "data": {"title_suggestion": None, "themes": None}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unknown_type_name_treated_as_free(self, mock_system_config):
        # a config typo in the type name must not punish the agent.
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"key_models": {"d": {"x": "strng"}}}))
        res = await srv.write({**SID, "doc": "d", "data": {"x": [1, 2]}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_boolean_not_counted_as_integer(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"key_models": {"d": {"n": "integer"}}}))
        assert (await srv.write({**SID, "doc": "d", "data": {"n": True}}))["status"] == "error"
        assert (await srv.write({**SID, "doc": "d", "data": {"n": 5},
                                 "if_exists": "replace"}))["status"] == "ok"


@pytest.fixture
def star_server(mock_system_config):
    """Server whose key model is keyed under '*' (default for ANY doc name) and
    uses a list model for an array-of-objects field."""
    return JsonStoreServer(
        "json_store", mock_system_config,
        ToolServerConfig(type="json_store", enabled=True, config={
            "key_models": {
                "*": {
                    "title_suggestion": "string",
                    "milestones": [{"label": "string", "setting": "string"}],
                },
            },
        }),
    )


class TestKeyModelDefaultAndList:
    @pytest.mark.asyncio
    async def test_star_model_validates_arbitrary_doc_name(self, star_server):
        # '*' applies to a doc name that has no explicit model (e.g. an auto-id).
        res = await star_server.write(
            {**SID, "doc": "doc_random123", "data": {"title_suggestion": ["A", "B"]}})
        assert res["status"] == "error"
        assert "title_suggestion" in res["error"] and "must be string" in res["error"]

    @pytest.mark.asyncio
    async def test_star_model_validates_auto_id_write(self, star_server):
        # Group-A fix: a write WITHOUT a doc name (auto-id) is validated too, so
        # the writer sees the type error on its own write and can self-correct.
        res = await star_server.write(
            {**SID, "data": {"title_suggestion": ["A", "B"]}})  # no doc -> auto-id
        assert res["status"] == "error"
        assert "title_suggestion" in res["error"]

    @pytest.mark.asyncio
    async def test_star_model_auto_id_valid_write_ok(self, star_server):
        res = await star_server.write({**SID, "data": {"title_suggestion": "Der Fund"}})
        assert res["status"] == "ok" and res["doc"].startswith("doc_")

    @pytest.mark.asyncio
    async def test_list_model_accepts_array_of_objects(self, star_server):
        res = await star_server.write(
            {**SID, "doc": "d",
             "data": {"milestones": [{"label": "Inciting", "setting": "Wald"}]}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_list_model_rejects_bare_object(self, star_server):
        # the array-coverage gap: a single object where an array belongs.
        res = await star_server.write(
            {**SID, "doc": "d",
             "data": {"milestones": {"label": "Inciting", "setting": "Wald"}}})
        assert res["status"] == "error"
        assert "milestones" in res["error"] and "must be array" in res["error"]

    @pytest.mark.asyncio
    async def test_list_model_validates_element_types(self, star_server):
        res = await star_server.write(
            {**SID, "doc": "d",
             "data": {"milestones": [{"label": ["nope"], "setting": "Wald"}]}})
        assert res["status"] == "error"
        assert "label" in res["error"] and "must be string" in res["error"]

    @pytest.mark.asyncio
    async def test_list_model_null_and_empty_ok(self, star_server):
        assert (await star_server.write(
            {**SID, "doc": "d1", "data": {"milestones": None}}))["status"] == "ok"
        assert (await star_server.write(
            {**SID, "doc": "d2", "data": {"milestones": []}}))["status"] == "ok"


COORD = {"_session_id": "coordinator", "namespace": "grp"}
WRITER_A = {"_session_id": "writer_a", "namespace": "grp"}
WRITER_B = {"_session_id": "writer_b", "namespace": "grp"}


class TestWriteProtection:
    """A document belongs to the session that created it.

    Live incident this guards against: three panel writers merged into and
    deleted keys from the coordinator's 'synopsis', shrinking it each round.
    """

    @pytest.mark.asyncio
    async def test_reads_are_free_for_everyone(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"genre": "Krimi"}})
        for reader in (WRITER_A, WRITER_B):
            res = await server.read({**reader, "doc": "synopsis"})
            assert res["status"] == "ok"
            assert json.loads(res["json"]) == {"genre": "Krimi"}
        assert (await server.outline({**WRITER_A, "doc": "synopsis"}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_foreign_merge_rejected(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"genre": "Krimi"}})
        res = await server.merge({**WRITER_A, "doc": "synopsis",
                                  "data": {"synopsis_text": "hijack"}})
        assert res["status"] == "error"
        assert "another agent" in res["error"]
        assert "omit 'doc'" in res["error"]  # tells the writer what to do instead
        # document untouched
        data = json.loads((await server.read({**COORD, "doc": "synopsis"}))["json"])
        assert data == {"genre": "Krimi"}

    @pytest.mark.asyncio
    async def test_all_foreign_mutations_rejected(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"a": {"b": 1}}})
        await server.write({**WRITER_A, "doc": "mine", "data": {"x": 1}})
        cases = [
            server.write({**WRITER_A, "doc": "synopsis", "data": {"x": 1},
                          "if_exists": "replace"}),
            server.merge({**WRITER_A, "doc": "synopsis", "data": {"x": 1}}),
            server.merge_doc({**WRITER_A, "doc": "synopsis", "source": "mine"}),
            server.set_value({**WRITER_A, "doc": "synopsis", "path": "a.b", "value": 9}),
            server.delete_keys({**WRITER_A, "doc": "synopsis", "paths": ["a"]}),
            server.delete_doc({**WRITER_A, "doc": "synopsis"}),
        ]
        for coro in cases:
            res = await coro
            assert res["status"] == "error" and "another agent" in res["error"]
        data = json.loads((await server.read({**COORD, "doc": "synopsis"}))["json"])
        assert data == {"a": {"b": 1}}

    @pytest.mark.asyncio
    async def test_foreign_write_reports_ownership_not_collision(self, server):
        # Ownership must be checked BEFORE if_exists: otherwise a foreign doc
        # answers "already exists, use if_exists='replace'" — an invitation to
        # retry that only then hits the owner check (one wasted turn).
        await server.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        res = await server.write({**WRITER_A, "doc": "synopsis", "data": {"b": 2}})
        assert res["status"] == "error"
        assert "another agent" in res["error"]
        assert "already exists" not in res["error"]

    @pytest.mark.asyncio
    async def test_own_doc_still_reports_collision(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        res = await server.write({**COORD, "doc": "synopsis", "data": {"b": 2}})
        assert res["status"] == "error" and "already exists" in res["error"]

    @pytest.mark.asyncio
    async def test_foreign_merge_not_masked_by_payload_error(self, server):
        # The guard runs before payload parsing, so the writer learns the real
        # reason instead of an "invalid JSON" red herring.
        await server.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        res = await server.merge({**WRITER_A, "doc": "synopsis",
                                  "json_text": "not json at all {"})
        assert res["status"] == "error" and "another agent" in res["error"]

    @pytest.mark.asyncio
    async def test_owner_may_do_everything(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"a": {"b": 1}}})
        assert (await server.merge({**COORD, "doc": "synopsis",
                                    "data": {"c": 2}}))["status"] == "ok"
        assert (await server.set_value({**COORD, "doc": "synopsis", "path": "a.b",
                                        "value": 9}))["status"] == "ok"
        assert (await server.delete_keys({**COORD, "doc": "synopsis",
                                          "paths": ["c"]}))["status"] == "ok"
        assert (await server.delete_doc({**COORD, "doc": "synopsis"}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_merge_doc_source_is_read_only(self, server):
        # The v6 flow: writer owns its delta, coordinator owns synopsis and
        # merges the (foreign) delta into it.
        await server.write({**COORD, "doc": "synopsis", "data": {"genre": "Krimi"}})
        delta = await server.write({**WRITER_A, "data": {"world_setting": "Hafen"}})
        res = await server.merge_doc({**COORD, "doc": "synopsis",
                                      "source": delta["doc"]})
        assert res["status"] == "ok"
        assert res["merged_keys"] == ["world_setting"]

    @pytest.mark.asyncio
    async def test_parallel_writers_own_their_own_deltas(self, server):
        a = await server.write({**WRITER_A, "data": {"x": 1}})
        b = await server.write({**WRITER_B, "data": {"y": 2}})
        assert a["doc"] != b["doc"]
        # A cannot touch B's delta ...
        res = await server.merge({**WRITER_A, "doc": b["doc"], "data": {"z": 3}})
        assert res["status"] == "error"
        # ... but may revise its own (same session across `continue`).
        res = await server.write({**WRITER_A, "doc": a["doc"], "data": {"x": 9},
                                  "if_exists": "replace"})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_shared_opt_out(self, server):
        await server.write({**COORD, "doc": "scratch", "data": {"a": 1},
                            "write_access": "shared"})
        res = await server.merge({**WRITER_A, "doc": "scratch", "data": {"b": 2}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_invalid_write_access(self, server):
        res = await server.write({**COORD, "doc": "d", "data": {},
                                  "write_access": "public"})
        assert res["status"] == "error" and "write_access" in res["error"]

    @pytest.mark.asyncio
    async def test_write_access_ignored_for_existing_doc(self, server):
        # A foreign agent cannot hijack a doc by claiming shared access.
        await server.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        res = await server.write({**WRITER_A, "doc": "synopsis", "data": {"a": 2},
                                  "if_exists": "replace", "write_access": "shared"})
        assert res["status"] == "error" and "another agent" in res["error"]

    @pytest.mark.asyncio
    async def test_default_shared_config_restores_old_behavior(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"default_write_access": "shared"}))
        await srv.write({**COORD, "doc": "d", "data": {"a": 1}})
        assert (await srv.merge({**WRITER_A, "doc": "d",
                                 "data": {"b": 2}}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_list_reports_writable(self, server):
        await server.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        await server.write({**WRITER_A, "doc": "mine", "data": {"a": 1}})
        docs = {d["doc"]: d["writable"]
                for d in (await server.list_docs(WRITER_A))["docs"]}
        assert docs == {"synopsis": False, "mine": True}

    @pytest.mark.asyncio
    async def test_delete_doc_releases_ownership(self, server):
        await server.write({**COORD, "doc": "d", "data": {"a": 1}})
        await server.delete_doc({**COORD, "doc": "d"})
        # name is free again for anyone
        assert (await server.write({**WRITER_A, "doc": "d",
                                    "data": {"b": 2}}))["status"] == "ok"
        assert (await server.merge({**COORD, "doc": "d",
                                    "data": {"c": 3}}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_sessionless_callers_share_ownership(self, server):
        # No _session_id -> "global": behaves like a single agent (tests, CLI).
        await server.write({"namespace": "n", "doc": "d", "data": {"a": 1}})
        assert (await server.merge({"namespace": "n", "doc": "d",
                                    "data": {"b": 2}}))["status"] == "ok"


class TestUndo:
    """Single-step, repeatable undo — the recovery path for a mistake, so the
    agent never has to re-type the previous JSON by hand."""

    @pytest.mark.asyncio
    async def test_undo_reverts_a_bad_merge(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})  # oops
        res = await server.undo({**SID, "doc": "d"})
        assert res["status"] == "ok" and res["undone"] == "merge"
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_undo_is_repeatable_across_steps(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})
        await server.merge({**SID, "doc": "d", "data": {"c": 3}})
        await server.undo({**SID, "doc": "d"})   # drop c
        await server.undo({**SID, "doc": "d"})   # drop b
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_undo_of_create_deletes_the_doc(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        res = await server.undo({**SID, "doc": "d"})
        assert res["status"] == "ok" and res["deleted"] is True
        assert (await server.read({**SID, "doc": "d"}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_undo_reverts_replace(self, server):
        await server.write({**SID, "doc": "d", "data": {"v": 1}})
        await server.write({**SID, "doc": "d", "data": {"v": 2}, "if_exists": "replace"})
        await server.undo({**SID, "doc": "d"})
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"v": 1}

    @pytest.mark.asyncio
    async def test_undo_reverts_set_value(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": {"b": 1}}})
        await server.set_value({**SID, "doc": "d", "path": "a.b", "value": 99})
        await server.undo({**SID, "doc": "d"})
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": {"b": 1}}

    @pytest.mark.asyncio
    async def test_undo_reverts_delete_keys(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1, "b": 2}})
        await server.delete_keys({**SID, "doc": "d", "paths": ["a"]})
        await server.undo({**SID, "doc": "d"})
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_undo_recreates_a_deleted_doc(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.delete_doc({**SID, "doc": "d"})
        res = await server.undo({**SID, "doc": "d"})
        assert res["status"] == "ok" and res["undone"] == "delete_doc"
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_undo_reverts_merge_doc(self, server):
        await server.write({**SID, "doc": "target", "data": {"a": 1}})
        await server.write({**SID, "doc": "src", "data": {"b": 2}})
        await server.merge_doc({**SID, "doc": "target", "source": "src"})
        await server.undo({**SID, "doc": "target"})
        assert json.loads((await server.read({**SID, "doc": "target"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_nothing_to_undo(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.undo({**SID, "doc": "d"})   # undo the create
        res = await server.undo({**SID, "doc": "d"})  # nothing left
        assert res["status"] == "error" and "Nothing to undo" in res["error"]

    @pytest.mark.asyncio
    async def test_snapshots_left_decreases(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})
        r1 = await server.undo({**SID, "doc": "d"})
        assert r1["snapshots_left"] == 1   # the create snapshot remains
        r2 = await server.undo({**SID, "doc": "d"})
        assert r2["snapshots_left"] == 0

    @pytest.mark.asyncio
    async def test_depth_caps_history(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={"undo_depth": 2}))
        await srv.write({**SID, "doc": "d", "data": {"n": 0}})
        for n in range(1, 5):
            await srv.merge({**SID, "doc": "d", "data": {"n": n}})
        # only the last 2 mutations are reversible
        await srv.undo({**SID, "doc": "d"})   # n=4 -> n=3
        await srv.undo({**SID, "doc": "d"})   # n=3 -> n=2
        assert json.loads((await srv.read({**SID, "doc": "d"}))["json"])["n"] == 2
        assert (await srv.undo({**SID, "doc": "d"}))["status"] == "error"

    @pytest.mark.asyncio
    async def test_undo_disabled(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={"undo_depth": 0}))
        await srv.write({**SID, "doc": "d", "data": {"a": 1}})
        await srv.merge({**SID, "doc": "d", "data": {"b": 2}})
        res = await srv.undo({**SID, "doc": "d"})
        assert res["status"] == "error" and "disabled" in res["error"]

    @pytest.mark.asyncio
    async def test_foreign_agent_cannot_undo(self, server):
        owner = {"_session_id": "owner", "namespace": "grp"}
        other = {"_session_id": "other", "namespace": "grp"}
        await server.write({**owner, "doc": "synopsis", "data": {"a": 1}})
        await server.merge({**owner, "doc": "synopsis", "data": {"b": 2}})
        res = await server.undo({**other, "doc": "synopsis"})
        assert res["status"] == "error" and "another agent" in res["error"]
        # owner still can
        assert (await server.undo({**owner, "doc": "synopsis"}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_foreign_agent_cannot_undo_a_deletion(self, server):
        owner = {"_session_id": "owner", "namespace": "grp"}
        other = {"_session_id": "other", "namespace": "grp"}
        await server.write({**owner, "doc": "d", "data": {"a": 1}})
        await server.delete_doc({**owner, "doc": "d"})
        # doc is gone; a foreign agent must not resurrect it
        res = await server.undo({**other, "doc": "d"})
        assert res["status"] == "error" and "another agent" in res["error"]

    @pytest.mark.asyncio
    async def test_noop_delete_keys_does_not_consume_undo_depth(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})   # real change
        # a no-op delete (all paths missing) must not evict the real snapshot
        for _ in range(5):
            r = await server.delete_keys({**SID, "doc": "d", "paths": ["nope"]})
            assert r["status"] == "ok" and r["missing"] == ["nope"]
        # undo still reverts the merge, not a no-op
        u = await server.undo({**SID, "doc": "d"})
        assert u["undone"] == "merge"
        assert json.loads((await server.read({**SID, "doc": "d"}))["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_noop_replace_does_not_snapshot(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})
        # replace with identical content = no-op
        await server.write({**SID, "doc": "d", "data": {"a": 1, "b": 2},
                            "if_exists": "replace"})
        u = await server.undo({**SID, "doc": "d"})
        assert u["undone"] == "merge"   # the no-op replace left no snapshot

    @pytest.mark.asyncio
    async def test_history_bounded_across_name_churn(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"max_docs": 10, "undo_depth": 5}))
        S = {"_session_id": "s", "namespace": "g"}
        # churn 200 distinct auto-id docs (create + delete) — history for dead
        # docs must not grow without bound.
        for _ in range(200):
            r = await srv.write({**S, "data": {"x": 1}})
            await srv.delete_doc({**S, "doc": r["doc"]})
        assert len(srv._doc_history["g"]) <= 2 * 10   # cap = 2x max_docs

    @pytest.mark.asyncio
    async def test_live_docs_keep_history_under_churn(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True,
                      config={"max_docs": 10, "undo_depth": 5}))
        S = {"_session_id": "s", "namespace": "g"}
        await srv.write({**S, "doc": "keep", "data": {"v": 0}})
        await srv.merge({**S, "doc": "keep", "data": {"w": 1}})
        # churn many dead docs — pruning must drop DEAD docs first, keep 'keep'
        for _ in range(100):
            r = await srv.write({**S, "data": {"x": 1}})
            await srv.delete_doc({**S, "doc": r["doc"]})
        u = await srv.undo({**S, "doc": "keep"})
        assert u["status"] == "ok" and u["undone"] == "merge"

    @pytest.mark.asyncio
    async def test_empty_history_entry_removed(self, server):
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.undo({**SID, "doc": "d"})     # undo the create, stack empties
        assert "d" not in server._doc_history.get("grp", {})
        assert "d" not in server._doc_history.get("sess-1", {})

    @pytest.mark.asyncio
    async def test_undo_status_event(self, server):
        st = RecordingStatus()
        await server.write({**SID, "doc": "d", "data": {"a": 1}})
        await server.merge({**SID, "doc": "d", "data": {"b": 2}})
        res = await server.manage_json({**SID, "_status": st, "operation": "undo",
                                        "doc": "d"})
        assert res["status"] == "ok"
        assert len(st.ended) == 1 and "undid merge" in st.ended[0]


class RecordingStatus:
    """Captures the terminal status event the plugin emits."""

    def __init__(self):
        self.ended = []
        self.errors = []

    async def progress(self, msg, meta=None):
        pass

    async def end(self, msg="completed", meta=None):
        self.ended.append(msg)

    async def error(self, msg, meta=None):
        self.errors.append(msg)


class TestStatusMessages:
    """Every operation must say WHAT it did — a bare "completed" leaves the
    operator guessing which of nine operations on which doc just finished."""

    @pytest.mark.asyncio
    async def test_every_operation_emits_a_descriptive_end(self, server):
        cases = [
            ({"operation": "write", "doc": "d", "data": {"a": 1}}, "wrote 'd'"),
            ({"operation": "read", "doc": "d"}, "read 'd'"),
            ({"operation": "merge", "doc": "d", "data": {"b": 2}}, "merged into 'd'"),
            ({"operation": "write", "doc": "src", "data": {"c": 3}}, "wrote 'src'"),
            ({"operation": "merge_doc", "doc": "d", "source": "src"},
             "merged 'src' → 'd'"),
            ({"operation": "set_value", "doc": "d", "path": "x.y", "value": 1},
             "set 'd'.x.y"),
            ({"operation": "outline", "doc": "d"}, "outline of 'd'"),
            ({"operation": "list"}, "listed"),
            ({"operation": "delete_keys", "doc": "d", "paths": ["a"]},
             "deleted 1 key(s) from 'd'"),
            ({"operation": "delete_doc", "doc": "d"}, "deleted doc 'd'"),
        ]
        for params, expected in cases:
            st = RecordingStatus()
            res = await server.manage_json({**SID, "_status": st, **params})
            assert res["status"] == "ok", (params, res)
            assert st.errors == []
            assert len(st.ended) == 1, f"{params} -> {st.ended}"
            assert expected in st.ended[0], f"{params} -> {st.ended[0]}"

    @pytest.mark.asyncio
    async def test_write_reports_replace_and_auto_id(self, server):
        st = RecordingStatus()
        res = await server.manage_json({**SID, "_status": st, "operation": "write",
                                        "data": {"a": 1}})  # no doc -> auto-id
        assert res["doc"] in st.ended[0] and "wrote" in st.ended[0]

        st2 = RecordingStatus()
        await server.manage_json({**SID, "_status": st2, "operation": "write",
                                  "doc": "d", "data": {"a": 1}})
        st3 = RecordingStatus()
        await server.manage_json({**SID, "_status": st3, "operation": "write",
                                  "doc": "d", "data": {"b": 2}, "if_exists": "replace"})
        assert "replaced 'd'" in st3.ended[0]

    @pytest.mark.asyncio
    async def test_merge_lists_the_merged_keys(self, server):
        await server.manage_json({**SID, "operation": "write", "doc": "d",
                                  "data": {"a": 1}})
        st = RecordingStatus()
        await server.manage_json({**SID, "_status": st, "operation": "merge",
                                  "doc": "d", "data": {"x": 1, "y": 2}})
        assert "x" in st.ended[0] and "y" in st.ended[0]

    @pytest.mark.asyncio
    async def test_failure_uses_error_not_end(self, server):
        # A failed operation must NOT show up as a green "completed".
        st = RecordingStatus()
        res = await server.manage_json({**SID, "_status": st, "operation": "read",
                                        "doc": "missing"})
        assert res["status"] == "error"
        assert st.ended == []
        assert len(st.errors) == 1
        assert "read 'missing' failed" in st.errors[0]
        assert "not found" in st.errors[0]

    @pytest.mark.asyncio
    async def test_unknown_operation_reports_error(self, server):
        st = RecordingStatus()
        res = await server.manage_json({**SID, "_status": st, "operation": "explode"})
        assert res["status"] == "error"
        assert st.ended == [] and st.errors == []  # no handler ran

    @pytest.mark.asyncio
    async def test_exactly_one_terminal_event(self, server):
        # Handlers must not emit their own end() on top of the dispatcher's.
        st = RecordingStatus()
        await server.manage_json({**SID, "_status": st, "operation": "write",
                                  "doc": "d", "data": {"a": 1}})
        assert len(st.ended) + len(st.errors) == 1

    @pytest.mark.asyncio
    async def test_works_without_status(self, server):
        res = await server.manage_json({**SID, "operation": "write", "doc": "d",
                                        "data": {"a": 1}})
        assert res["status"] == "ok"


class TestKeyAliases:
    @pytest.fixture
    def aliased_server(self, mock_system_config):
        return JsonStoreServer(
            "json_store", mock_system_config,
            ToolServerConfig(type="json_store", enabled=True, config={
                "key_aliases": {"synopsis": "synopsis_text"},
                "key_models": {"synopsis": {"synopsis_text": {}, "genre": {}}},
            }),
        )

    @pytest.mark.asyncio
    async def test_explicit_alias_remaps_semantic_rename(self, aliased_server):
        # synopsis→synopsis_text is a semantic rename fuzzy would miss; alias fixes it
        res = await aliased_server.write(
            {**SID, "doc": "synopsis", "data": {"synopsis": "die Story", "genre": "X"}})
        assert res["status"] == "ok"
        assert any("synopsis → synopsis_text" in r for r in res["remapped"])
        data = json.loads((await aliased_server.read({**SID, "doc": "synopsis"}))["json"])
        assert data == {"synopsis_text": "die Story", "genre": "X"}

    @pytest.mark.asyncio
    async def test_alias_collision_not_applied(self, aliased_server):
        # both 'synopsis' and 'synopsis_text' present → don't clobber, reject
        res = await aliased_server.write(
            {**SID, "doc": "synopsis",
             "data": {"synopsis": "A", "synopsis_text": "B"}})
        assert res["status"] == "error"


# ---------------------------------------------------------------------------
# realistic coordinator scenario
# ---------------------------------------------------------------------------

class TestSynopsisWorkflow:
    @pytest.mark.asyncio
    async def test_multi_step_merge_stays_valid(self, server):
        """Simulates the v6 coordinator: sub-agent step results are merged in
        code; the final document is complete and valid without the LLM ever
        re-typing JSON."""
        # 2.1 Idea (json_text with fence + raw newline, as LLMs produce it)
        idea = '```json\n{"synopsis": "Zeile 1\nZeile 2", "genre": "SciFi"}\n```'
        assert (await server.write(
            {**SID, "doc": "synopsis", "json_text": idea}))["status"] == "ok"

        # 2.2 World adds fields and refines synopsis
        assert (await server.merge(
            {**SID, "doc": "synopsis",
             "data": {"world_setting": {"location": "Fjord"}, "synopsis": "verfeinert"}}
        ))["status"] == "ok"

        # 2.3 Characters
        assert (await server.merge(
            {**SID, "doc": "synopsis",
             "data": {"key_characters": {"Nora": {"age": 34}}}}
        ))["status"] == "ok"

        final = await server.read({**SID, "doc": "synopsis"})
        data = json.loads(final["json"])  # guaranteed parseable
        assert set(data.keys()) == {"synopsis", "genre", "world_setting", "key_characters"}
        assert data["synopsis"] == "verfeinert"
        assert data["genre"] == "SciFi"


# ---------------------------------------------------------------------------
# disk persistence (abort + continue / server restart)
# ---------------------------------------------------------------------------

def _restartable(mock_system_config, tmp_path, **cfg):
    """A server whose storage survives into the next instance (same path)."""
    cfg.setdefault("storage_path", str(tmp_path / "jsstore"))
    return JsonStoreServer(
        "json_store", mock_system_config,
        ToolServerConfig(type="json_store", enabled=True, config=cfg))


class TestPersistence:
    @pytest.mark.asyncio
    async def test_docs_survive_restart(self, mock_system_config, tmp_path):
        # The live failure: CLI abort + continue = new process = new instance.
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "synopsis", "data": {"genre": "Krimi"}})
        await a.merge({**COORD, "doc": "synopsis",
                       "data": {"key_characters": {"Nora": {"age": 34}}}})
        b = _restartable(mock_system_config, tmp_path)
        res = await b.read({**COORD, "doc": "synopsis"})
        assert res["status"] == "ok"
        assert json.loads(res["json"]) == {
            "genre": "Krimi", "key_characters": {"Nora": {"age": 34}}}

    @pytest.mark.asyncio
    async def test_owner_survives_restart(self, mock_system_config, tmp_path):
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "synopsis", "data": {"a": 1}})
        b = _restartable(mock_system_config, tmp_path)
        # foreign session still read-only after the "restart" ...
        res = await b.merge({**WRITER_A, "doc": "synopsis", "data": {"b": 2}})
        assert res["status"] == "error" and "another agent" in res["error"]
        # ... the owning session (restored on continue) may keep writing.
        assert (await b.merge({**COORD, "doc": "synopsis",
                               "data": {"b": 2}}))["status"] == "ok"

    @pytest.mark.asyncio
    async def test_delete_doc_removes_file(self, mock_system_config, tmp_path):
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "gone", "data": {"a": 1}})
        await a.delete_doc({**COORD, "doc": "gone"})
        b = _restartable(mock_system_config, tmp_path)
        res = await b.read({**COORD, "doc": "gone"})
        assert res["status"] == "error" and "not found" in res["error"]

    @pytest.mark.asyncio
    async def test_delete_keys_persisted(self, mock_system_config, tmp_path):
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "beats",
                       "data": {"B01": {"title": "x"}, "B02": {"title": "y"}}})
        await a.delete_keys({**COORD, "doc": "beats", "paths": ["B02"]})
        b = _restartable(mock_system_config, tmp_path)
        assert json.loads((await b.read({**COORD, "doc": "beats"}))["json"]) == {
            "B01": {"title": "x"}}

    @pytest.mark.asyncio
    async def test_unsafe_names_roundtrip_without_escaping_storage(
            self, mock_system_config, tmp_path):
        # Namespace and doc names are LLM-supplied — path traversal must be
        # neutralized, and the original names must still round-trip.
        a = _restartable(mock_system_config, tmp_path)
        nasty = {"_session_id": "s1", "namespace": "../../und /zurück"}
        await a.write({**nasty, "doc": "syn:opsis?", "data": {"ok": True}})
        files = [p for p in tmp_path.rglob("*.json")]
        assert files and all(
            (tmp_path / "jsstore") in p.parents for p in files)
        b = _restartable(mock_system_config, tmp_path)
        res = await b.read({**nasty, "doc": "syn:opsis?"})
        assert json.loads(res["json"]) == {"ok": True}

    @pytest.mark.asyncio
    async def test_persist_false_writes_nothing(self, mock_system_config, tmp_path):
        srv = _restartable(mock_system_config, tmp_path, persist=False)
        await srv.write({**COORD, "doc": "d", "data": {"a": 1}})
        assert not (tmp_path / "jsstore").exists()

    @pytest.mark.asyncio
    async def test_case_only_name_variants_get_distinct_files(
            self, mock_system_config, tmp_path):
        # NTFS/macOS paths are case-INsensitive: 'Run7' and 'run7' are two
        # namespaces in memory but would be ONE directory without the hash
        # suffix — the second write would clobber the first's file.
        a = _restartable(mock_system_config, tmp_path)
        await a.write({"_session_id": "s1", "namespace": "Run7",
                       "doc": "d", "data": {"who": "upper"}})
        await a.write({"_session_id": "s2", "namespace": "run7",
                       "doc": "d", "data": {"who": "lower"}})
        # same for doc names within one namespace
        await a.write({**COORD, "doc": "Beat1", "data": {"who": "upper"}})
        await a.write({**COORD, "doc": "beat1", "data": {"who": "lower"}})
        b = _restartable(mock_system_config, tmp_path)
        for ns, who in (("Run7", "upper"), ("run7", "lower")):
            res = await b.read({"_session_id": "sx", "namespace": ns, "doc": "d"})
            assert json.loads(res["json"]) == {"who": who}
        for doc, who in (("Beat1", "upper"), ("beat1", "lower")):
            res = await b.read({**COORD, "doc": doc})
            assert json.loads(res["json"]) == {"who": who}

    @pytest.mark.asyncio
    async def test_trailing_dot_namespace_gets_its_own_directory(
            self, mock_system_config, tmp_path):
        # Windows drops a trailing '.', so 'run.' used to open the directory
        # of 'run' and read its documents.
        assert not JsonStoreServer._safe_filename("run.").endswith(".")
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "namespace": "run", "doc": "d", "data": {"a": 1}})
        b = _restartable(mock_system_config, tmp_path)
        res = await b.list_docs({**COORD, "namespace": "run."})
        assert res["count"] == 0, res

    @pytest.mark.asyncio
    async def test_sharing_violation_on_replace_and_read_is_retried(
            self, mock_system_config, tmp_path, monkeypatch):
        # Windows: os.replace onto a file another process reads, and a read
        # during a replace, raise PermissionError for a moment.
        import pathlib
        from plugins.json_store import server as mod
        monkeypatch.setattr(mod, "_SHARING_PAUSE_S", 0)
        monkeypatch.setattr(mod, "_RETRY_SHARING", True)   # the Windows behaviour, on any OS

        def flaky(real, fails):
            def call(*args, **kwargs):
                if fails:
                    fails.pop()
                    raise PermissionError(13, "sharing violation")
                return real(*args, **kwargs)
            return call

        a = _restartable(mock_system_config, tmp_path)
        monkeypatch.setattr(mod.os, "replace", flaky(mod.os.replace, [1, 1, 1]))
        res = await a.write({**COORD, "doc": "d", "data": {"a": 1}})
        assert "persist_error" not in res, res
        monkeypatch.setattr(pathlib.Path, "read_text",
                            flaky(pathlib.Path.read_text, [1, 1, 1]))
        b = _restartable(mock_system_config, tmp_path)
        assert json.loads((await b.read({**COORD, "doc": "d"}))["json"]) == {"a": 1}
        assert not list(tmp_path.rglob("*.tmp"))

    @pytest.mark.asyncio
    async def test_a_failed_file_delete_does_not_bring_the_doc_back(
            self, mock_system_config, tmp_path, monkeypatch):
        import pathlib
        from plugins.json_store import server as mod
        monkeypatch.setattr(mod, "_SHARING_PAUSE_S", 0)
        monkeypatch.setattr(mod, "_RETRY_SHARING", True)
        a = _restartable(mock_system_config, tmp_path)
        op = lambda **p: a.manage_json({**COORD, **p})  # noqa: E731
        await op(operation="write", doc="stuck", data={"a": 1})
        await op(operation="write", doc="brief", data={"a": 1})
        real_unlink = pathlib.Path.unlink
        fails = {"brief": 3, "stuck": 10 ** 6}   # a short lock, a lasting one

        def unlink(self, *args, **kwargs):
            for stem, left in fails.items():
                if self.name == f"{stem}.json" and left:
                    fails[stem] -= 1
                    raise PermissionError(13, "sharing violation")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", unlink)
        res = await op(operation="delete_doc", doc="brief")
        assert res["status"] == "ok" and "persist_error" not in res, res
        res = await op(operation="delete_doc", doc="stuck")
        assert "persist_error" in res, res
        assert (await op(operation="list"))["count"] == 0
        files = sorted(p.name for p in tmp_path.rglob("*.json"))
        assert files == ["stuck.json"], files

    @pytest.mark.asyncio
    async def test_permission_error_off_windows_is_not_retried(
            self, mock_system_config, tmp_path, monkeypatch):
        from plugins.json_store import server as mod
        monkeypatch.setattr(mod, "_RETRY_SHARING", False)
        calls = []

        def replace(*args):
            calls.append(args)
            raise PermissionError(13, "denied")

        a = _restartable(mock_system_config, tmp_path)
        monkeypatch.setattr(mod.os, "replace", replace)
        res = await a.write({**COORD, "doc": "d", "data": {"a": 1}})
        assert "persist_error" in res and len(calls) == 1, (res, len(calls))

    @pytest.mark.asyncio
    async def test_a_file_replaced_right_after_ours_is_not_taken_for_ours(
            self, mock_system_config, tmp_path, monkeypatch):
        # Another process replaces the file between our replace and anything
        # we do next, with the same size and even the same mtime: only the
        # file's identity tells it apart.
        import os
        from plugins.json_store import server as mod
        real_replace = mod.os.replace
        other = {"on": False}

        def replace(src, dst):
            real_replace(src, dst)
            if other["on"]:
                other["on"] = False
                mtime = os.stat(dst).st_mtime_ns
                foreign = str(dst) + ".other"
                with open(foreign, "w", encoding="utf-8") as fh:
                    json.dump({"namespace": "grp", "doc": "d",
                               "owner": None, "data": {"v": "b"}}, fh, ensure_ascii=False)
                assert os.path.getsize(foreign) == os.path.getsize(dst)
                os.utime(foreign, ns=(mtime, mtime))
                real_replace(foreign, dst)

        monkeypatch.setattr(mod.os, "replace", replace)
        a = _restartable(mock_system_config, tmp_path)
        await a.manage_json({**COORD, "operation": "write", "doc": "d",
                             "data": {"v": "a"}, "write_access": "shared"})
        other["on"] = True
        await a.manage_json({**COORD, "operation": "set_value", "doc": "d",
                             "path": "v", "value": "c"})
        res = await a.manage_json({**COORD, "operation": "read", "doc": "d"})
        assert json.loads(res["json"]) == {"v": "b"}, res

    @pytest.mark.asyncio
    async def test_files_under_the_old_trailing_dot_names_are_migrated(
            self, mock_system_config, tmp_path):
        store = tmp_path / "jsstore" / "json_store"

        def put(folder, ns, doc, fname):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / fname).write_text(json.dumps(
                {"namespace": ns, "doc": doc, "owner": None, "data": {"ns": ns}}),
                encoding="utf-8")

        # namespace 'run.' in its old folder (on Windows that IS 'run'), next
        # to a document of 'run'; a document 'a.' under its old file name
        legacy = store / JsonStoreServer._safe_filename("run.", hash_trailing_dot=False)
        put(legacy, "run.", "x", "x.json")
        put(store / "run", "run", "y", "y.json")
        put(store / "grp", "grp", "a.", "a..json")
        a = _restartable(mock_system_config, tmp_path)
        op = lambda **p: a.manage_json({"_session_id": "s", **p})  # noqa: E731
        assert [d["doc"] for d in (await op(operation="list", namespace="run"))["docs"]] == ["y"]
        assert [d["doc"] for d in (await op(operation="list", namespace="run."))["docs"]] == ["x"]
        assert json.loads((await op(operation="read", namespace="grp", doc="a."))["json"]) == {
            "ns": "grp"}
        names = sorted(str(p.relative_to(store)).replace("\\", "/")
                       for p in store.rglob("*.json"))
        assert names == sorted([
            f"{JsonStoreServer._safe_filename('run.')}/x.json", "run/y.json",
            f"grp/{JsonStoreServer._safe_filename('a.')}.json"]), names
        b = _restartable(mock_system_config, tmp_path)
        assert (await b.manage_json({"_session_id": "s", "operation": "list",
                                     "namespace": "run."}))["count"] == 1

    @staticmethod
    def _put(folder, ns, doc, fname, data, mtime_ns=None):
        import os
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / fname
        path.write_text(json.dumps({"namespace": ns, "doc": doc, "owner": None,
                                    "data": data}), encoding="utf-8")
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    @pytest.mark.parametrize("hard_links", [True, False])
    @pytest.mark.parametrize("old_is_newer", [True, False])
    @pytest.mark.parametrize("where", ["doc_name", "ns_folder"])
    @pytest.mark.asyncio
    async def test_when_old_and_new_file_both_exist_the_later_one_wins(
            self, mock_system_config, tmp_path, monkeypatch, old_is_newer, where,
            hard_links):
        # A process still running old code keeps writing the old name.
        if not hard_links:
            from plugins.json_store import server as mod

            def no_link(*args):
                raise OSError(1, "hard links not supported")

            monkeypatch.setattr(mod.os, "link", no_link)
        store = tmp_path / "jsstore" / "json_store"
        ns, doc = ("grp", "a.") if where == "doc_name" else ("run.", "x")
        new_dir = store / JsonStoreServer._safe_filename(ns)
        old_dir = new_dir if where == "doc_name" else (
            store / JsonStoreServer._safe_filename(ns, hash_trailing_dot=False))
        old_name = "a..json" if where == "doc_name" else "x.json"
        import time
        # recent times: the startup retention sweep deletes folders idle for days
        early, late = time.time_ns() - 60 * 10 ** 9, time.time_ns()
        t_old, t_new = (late, early) if old_is_newer else (early, late)
        self._put(old_dir, ns, doc, old_name, {"v": "old"}, t_old)
        self._put(new_dir, ns, doc, JsonStoreServer._safe_filename(doc) + ".json",
                  {"v": "new"}, t_new)
        a = _restartable(mock_system_config, tmp_path)
        res = await a.manage_json({"_session_id": "s", "namespace": ns,
                                   "operation": "read", "doc": doc})
        assert json.loads(res["json"]) == {"v": "old" if old_is_newer else "new"}, res
        assert not (old_dir / old_name).exists()

    @pytest.mark.asyncio
    async def test_migration_never_overwrites_a_file_that_appeared_meanwhile(
            self, mock_system_config, tmp_path, monkeypatch):
        # Another process migrates (and writes newer data) between our check
        # and our write: the move must not overwrite it.
        import pathlib
        store = tmp_path / "jsstore" / "json_store" / "grp"
        target = store / (JsonStoreServer._safe_filename("a.") + ".json")
        import time
        now = time.time_ns()
        self._put(store, "grp", "a.", "a..json", {"v": "old"}, now - 60 * 10 ** 9)
        self._put(store, "grp", "a.", target.name, {"v": "theirs"}, now)
        real_exists = pathlib.Path.exists
        monkeypatch.setattr(pathlib.Path, "exists",
                            lambda p, *a, **k: False if p.name == target.name
                            else real_exists(p, *a, **k))
        a = _restartable(mock_system_config, tmp_path)
        res = await a.manage_json({**COORD, "operation": "read", "doc": "a."})
        assert json.loads(res["json"]) == {"v": "theirs"}, res

    @pytest.mark.asyncio
    async def test_migration_without_hard_links_still_moves(
            self, mock_system_config, tmp_path, monkeypatch):
        from plugins.json_store import server as mod

        def no_link(*args):
            raise OSError(1, "hard links not supported")

        monkeypatch.setattr(mod.os, "link", no_link)
        store = tmp_path / "jsstore" / "json_store" / "grp"
        self._put(store, "grp", "a.", "a..json", {"v": "old"})
        a = _restartable(mock_system_config, tmp_path)
        res = await a.manage_json({**COORD, "operation": "read", "doc": "a."})
        assert json.loads(res["json"]) == {"v": "old"}
        assert sorted(p.name for p in store.iterdir()) == [
            JsonStoreServer._safe_filename("a.") + ".json"]

    @pytest.mark.asyncio
    async def test_migration_keeps_the_folder_windows_shares_with_the_old_name(
            self, mock_system_config, tmp_path):
        # On Windows the old folder of 'run.' IS the folder of 'run'.
        store = tmp_path / "jsstore" / "json_store"
        (store / "run").mkdir(parents=True)
        legacy = store / JsonStoreServer._safe_filename("run.", hash_trailing_dot=False)
        self._put(legacy, "run.", "x", "x.json", {"v": 1})
        a = _restartable(mock_system_config, tmp_path)
        await a.manage_json({"_session_id": "s", "namespace": "run.", "operation": "list"})
        assert (store / "run").is_dir()

    @pytest.mark.asyncio
    async def test_two_processes_keep_each_others_changes(
            self, mock_system_config, tmp_path):
        # Two instances on one storage = two processes (API + CLI run). Each
        # used to keep what it had loaded and overwrite the other's changes.
        a = _restartable(mock_system_config, tmp_path)
        b = _restartable(mock_system_config, tmp_path)
        op = lambda srv, **p: srv.manage_json({**COORD, **p})  # noqa: E731
        await op(a, operation="write", doc="d", data={"a": 1})
        await op(a, operation="write", doc="gone", data={"x": 1})
        assert (await op(b, operation="merge", doc="d", data={"bb": 2}))["status"] == "ok"
        assert (await op(b, operation="delete_doc", doc="gone"))["status"] == "ok"
        assert (await op(a, operation="merge", doc="d", data={"ccc": 3}))["status"] == "ok"
        # A's snapshots describe a state that is no longer on disk
        assert (await op(a, operation="undo", doc="d"))["status"] == "ok"   # its own merge
        assert (await op(a, operation="undo", doc="d"))["status"] == "error"
        await op(a, operation="merge", doc="d", data={"ccc": 3})
        assert [d["doc"] for d in (await op(a, operation="list"))["docs"]] == ["d"]
        c = _restartable(mock_system_config, tmp_path)
        assert json.loads((await op(c, operation="read", doc="d"))["json"]) == {
            "a": 1, "bb": 2, "ccc": 3}

    @pytest.mark.asyncio
    async def test_ttl_evicts_memory_but_reloads_from_disk(
            self, mock_system_config, tmp_path):
        import time as _time
        srv = _restartable(mock_system_config, tmp_path)
        await srv.write({"_session_id": "s1", "namespace": "A",
                         "doc": "d", "data": {"a": 1}})
        # backdate 'A' past the TTL, then touch another namespace → evicted
        srv._ns_last_access["A"] = _time.time() - srv._namespace_ttl_s - 1
        await srv.write({"_session_id": "s1", "namespace": "B",
                         "doc": "d", "data": {"b": 2}})
        assert "A" not in srv._docs
        res = await srv.read({"_session_id": "s1", "namespace": "A", "doc": "d"})
        assert res["status"] == "ok" and json.loads(res["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_retention_sweep_drops_old_namespaces(
            self, mock_system_config, tmp_path):
        import os as _os
        import time as _time
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "old", "data": {"a": 1}})
        old = _time.time() - 2 * 3600
        for f in (tmp_path / "jsstore").rglob("*.json"):
            _os.utime(f, (old, old))
        b = _restartable(mock_system_config, tmp_path, file_retention_hours=1)
        res = await b.read({**COORD, "doc": "old"})
        assert res["status"] == "error" and "not found" in res["error"]

    @pytest.mark.asyncio
    async def test_foreign_malformed_file_never_fatal(
            self, mock_system_config, tmp_path):
        # A hand-placed / drifted file (valid JSON, but not our dict payload)
        # must be skipped with a warning — not take the namespace down.
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "good", "data": {"a": 1}})
        ns_dir = next(d for d in (tmp_path / "jsstore" / "json_store").iterdir()
                      if d.is_dir())
        (ns_dir / "bad.json").write_text("null", encoding="utf-8")
        (ns_dir / "worse.json").write_text("[1, 2", encoding="utf-8")
        b = _restartable(mock_system_config, tmp_path)
        res = await b.read({**COORD, "doc": "good"})
        assert res["status"] == "ok" and json.loads(res["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_lone_surrogates_roundtrip(self, mock_system_config, tmp_path):
        # Parsed LLM JSON can carry lone surrogates in names and values —
        # persistence must neither raise nor silently stay memory-only.
        a = _restartable(mock_system_config, tmp_path)
        res = await a.write({"_session_id": "s1", "namespace": "ns\ud800x",
                             "doc": "d\ud800c", "data": {"t": "a\ud800b"}})
        assert res["status"] == "ok" and "persist_error" not in res
        b = _restartable(mock_system_config, tmp_path)
        res = await b.read({"_session_id": "s1", "namespace": "ns\ud800x",
                            "doc": "d\ud800c"})
        assert res["status"] == "ok"
        assert json.loads(res["json"]) == {"t": "a\ud800b"}

    @pytest.mark.asyncio
    async def test_undo_history_is_volatile(self, mock_system_config, tmp_path):
        a = _restartable(mock_system_config, tmp_path)
        await a.write({**COORD, "doc": "d", "data": {"a": 1}})
        await a.merge({**COORD, "doc": "d", "data": {"b": 2}})
        b = _restartable(mock_system_config, tmp_path)
        res = await b.undo({**COORD, "doc": "d"})
        assert res["status"] == "error" and "Nothing to undo" in res["error"]


# ---------------------------------------------------------------------------
# stats (Saettigungs-Analyse, writer O9c)
# ---------------------------------------------------------------------------

class TestStats:
    async def _seed_beats(self, server):
        await server.write({**SID, "doc": "beats", "data": {"beats": {
            "B01": {"summary": "Mila zeigt die Stoffmaus am Fenster",
                    "key_moments": ["Stoffmaus faellt vom Treppchen"]},
            "B02": {"summary": "Training mit der Stoffmaus im Hinterhof",
                    "key_moments": ["Moppel ignoriert die Stoffmaus"]},
            "B03": {"summary": "Der Baecker spricht ueber den Bauhof",
                    "key_moments": ["Ein Brief liegt auf der Theke"]},
        }}})

    @pytest.mark.asyncio
    async def test_recurring_terms_per_child(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats"})
        assert res["status"] == "ok"
        assert res["n_children"] == 3
        terms = {e["term"]: e["children"] for e in res["recurring_terms"]}
        # Stoffmaus traegt B01+B02 (2 Kinder), NICHT B03
        assert terms.get("stoffmaus") == 2
        # Einmal-Begriffe erscheinen nicht
        assert "bauhof" not in terms

    @pytest.mark.asyncio
    async def test_exclude_filters_names(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats", "exclude": ["stoffmaus"]})
        terms = {e["term"] for e in res["recurring_terms"]}
        assert not any("stoffmaus" in t for t in terms)

    @pytest.mark.asyncio
    async def test_exclude_accepts_comma_string(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats", "exclude": "stoffmaus, moppel"})
        terms = {e["term"] for e in res["recurring_terms"]}
        assert not any("stoffmaus" in t or "moppel" in t for t in terms)

    @pytest.mark.asyncio
    async def test_bad_path_and_scalar_path_error(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "gibtsnicht"})
        assert res["status"] == "error"
        await server.write({**SID, "doc": "flat", "data": {"x": "nur ein string"}})
        res = await server.stats({**SID, "operation": "stats", "doc": "flat",
                                  "path": "x"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_dispatch_via_manage_json(self, server):
        await self._seed_beats(server)
        res = await server.manage_json({**SID, "operation": "stats",
                                        "doc": "beats", "path": "beats"})
        assert res["status"] == "ok"
        assert any(e["term"] == "stoffmaus" for e in res["recurring_terms"])

    @pytest.mark.asyncio
    async def test_umlaut_stopwords_filtered(self, server):
        await server.write({**SID, "doc": "b2", "data": {"beats": {
            "B01": {"s": "Über die Brücke während der Nacht zur Stoffmaus"},
            "B02": {"s": "Über den Hof während des Tages zur Stoffmaus"},
        }}})
        res = await server.stats({**SID, "operation": "stats", "doc": "b2",
                                  "path": "beats"})
        terms = {e["term"] for e in res["recurring_terms"]}
        assert "über" not in terms and "während" not in terms
        assert "stoffmaus" in terms

    @pytest.mark.asyncio
    async def test_exclude_empty_and_short_entries_ignored(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats", "exclude": ["", "mo"]})
        # ""/Kurzst-Eintraege duerfen NICHT alles wegfiltern
        assert any(e["term"] == "stoffmaus" for e in res["recurring_terms"])

    @pytest.mark.asyncio
    async def test_exclude_scalar_is_param_error(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats", "exclude": 5})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_non_numeric_limits_are_param_error(self, server):
        await self._seed_beats(server)
        res = await server.stats({**SID, "operation": "stats", "doc": "beats",
                                  "path": "beats", "min_children": "alle"})
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_path_array_index_syntax(self, server):
        await server.write({**SID, "doc": "arr", "data": {"phasen": [
            {"beats": {"X": {"s": "Stoffmaus hier"}, "Y": {"s": "Stoffmaus dort"}}},
        ]}})
        res = await server.stats({**SID, "operation": "stats", "doc": "arr",
                                  "path": "phasen[0].beats"})
        assert res["status"] == "ok"
        assert any(e["term"] == "stoffmaus" for e in res["recurring_terms"])


class TestNotFoundRecovery:
    """A doc/path miss carries its own correction material — the agent should
    not need a list_docs turn to recover from a typo."""

    @pytest.mark.asyncio
    async def test_doc_typo_gets_suggestion_and_listing(self, server):
        await server.write({**SID, "doc": "beat_outline", "data": {"a": 1}})
        res = await server.read({**SID, "doc": "beat_outlien"})
        assert res["status"] == "error"
        assert res["did_you_mean"] == "beat_outline"
        assert "beat_outline" in res["existing"]

    @pytest.mark.asyncio
    async def test_unrelated_doc_name_lists_but_does_not_guess(self, server):
        await server.write({**SID, "doc": "beat_outline", "data": {"a": 1}})
        res = await server.read({**SID, "doc": "voellig_anders"})
        assert res["status"] == "error"
        assert res["did_you_mean"] is None
        assert res["existing"] == ["beat_outline"]

    @pytest.mark.asyncio
    async def test_path_segment_typo_names_the_neighbour(self, server):
        await server.write({**SID, "doc": "d",
                            "data": {"chapters": {"one": 1}, "meta": 2}})
        res = await server.read({**SID, "doc": "d", "path": "chapers"})
        assert res["status"] == "error"
        assert "did you mean 'chapters'" in res["error"]
        assert "available keys" in res["error"]

    @pytest.mark.asyncio
    async def test_stats_keeps_the_informative_path_error(self, server):
        """stats used to catch _resolve's rich ValueError and replace it with a
        generic line — silently discarding the keys the agent needed. (outline
        takes no path at all; stats is the path-walking inspector.)"""
        await server.write({**SID, "doc": "d", "data": {"beats": {"b1": {}}}})
        res = await server.stats({**SID, "doc": "d", "path": "beat"})
        assert res["status"] == "error"
        assert "available keys" in res["error"]
        assert "beats" in res["error"]


# ---------------------------------------------------------------------------
# require_namespace — the silent private-fallback guard
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_only_server(mock_system_config):
    """A store that exists ONLY as a shared workspace."""
    return JsonStoreServer(
        "json_store", mock_system_config,
        ToolServerConfig(type="json_store", enabled=True,
                  config={"require_namespace": True}),
    )


class TestRequireNamespace:
    """Without the flag a missing ``namespace`` scopes the document to the
    caller's own session — correct for a general store, wrong and INVISIBLE
    for a shared one: the write succeeds, returns a doc id, and every later
    read (which does pass the namespace) misses it.

    Measured on a v6 beat panel: one writer omitted the namespace five times,
    each ~11 KB, five failed read-backs, ~3 minutes and five LLM turns burnt.
    """

    @pytest.mark.asyncio
    async def test_write_without_namespace_is_rejected(self, shared_only_server):
        res = await shared_only_server.manage_json(
            {"operation": "write", "data": {"a": 1}, **SID},
        )
        assert res["status"] == "error"
        assert "namespace" in res["error"]

    @pytest.mark.asyncio
    async def test_every_operation_is_covered_not_just_write(
        self, shared_only_server,
    ):
        """Ein Lesen im falschen Namespace ist genauso still wie ein
        Schreiben — der Riegel sitzt deshalb vor dem Dispatch."""
        for op in ("read", "list", "outline", "stats", "delete_doc", "undo"):
            res = await shared_only_server.manage_json(
                {"operation": op, "doc": "d", **SID},
            )
            assert res["status"] == "error", op
            assert "namespace" in res["error"], op

    @pytest.mark.asyncio
    async def test_leerer_namespace_zaehlt_als_fehlend(self, shared_only_server):
        res = await shared_only_server.manage_json(
            {"operation": "write", "namespace": "", "data": {"a": 1}, **SID},
        )
        assert res["status"] == "error"

    @pytest.mark.asyncio
    async def test_mit_namespace_laeuft_es_durch(self, shared_only_server):
        wrote = await shared_only_server.manage_json(
            {"operation": "write", "namespace": "16459", "data": {"a": 1}, **SID},
        )
        assert wrote["status"] == "ok", wrote
        # Der gemeldete Doc-Name muss auch auflösbar sein — genau das war
        # im Fehlerfall nicht so.
        back = await shared_only_server.manage_json(
            {"operation": "read", "namespace": "16459",
             "doc": wrote["doc"], **SID},
        )
        assert back["status"] == "ok"
        assert json.loads(back["json"]) == {"a": 1}

    @pytest.mark.asyncio
    async def test_ohne_flag_bleibt_das_alte_verhalten(self, server):
        """Der generische Store darf sich nicht ändern — die
        Session-Skopierung ist dort ein Feature."""
        res = await server.manage_json(
            {"operation": "write", "data": {"a": 1}, **SID},
        )
        assert res["status"] == "ok"
