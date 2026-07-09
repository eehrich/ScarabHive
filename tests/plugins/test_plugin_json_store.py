"""Tests for json_store plugin — validated JSON working documents."""

import json

import pytest
from unittest.mock import MagicMock

from plugins.json_store.server import JsonStoreServer
from agent_system.config.models import MCPConfig


@pytest.fixture
def mock_system_config():
    config = MagicMock()
    config.ssl_verify = True
    return config


@pytest.fixture
def server(mock_system_config):
    return JsonStoreServer(
        "json_store", mock_system_config, MCPConfig(type="json_store", enabled=True)
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
            MCPConfig(type="json_store", enabled=True, config={"max_docs": 1}),
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
            MCPConfig(type="json_store", enabled=True,
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
    async def test_namespace_ttl_eviction(self, server):
        import time as _time
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
            MCPConfig(type="json_store", enabled=True, config={"max_doc_bytes": 50}),
        )
        res = await small.write({**SID, "doc": "big", "data": {"t": "x" * 100}})
        assert res["status"] == "error"
        assert "too large" in res["error"].lower()


# ---------------------------------------------------------------------------
# manage_json dispatcher (the actual exposed tool)
# ---------------------------------------------------------------------------

class TestManageJsonDispatch:
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
        MCPConfig(type="json_store", enabled=True, config={
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
        MCPConfig(type="json_store", enabled=True, config={
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
            MCPConfig(type="json_store", enabled=True,
                      config={"key_models": {"d": {"x": "strng"}}}))
        res = await srv.write({**SID, "doc": "d", "data": {"x": [1, 2]}})
        assert res["status"] == "ok"

    @pytest.mark.asyncio
    async def test_boolean_not_counted_as_integer(self, mock_system_config):
        srv = JsonStoreServer(
            "json_store", mock_system_config,
            MCPConfig(type="json_store", enabled=True,
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
        MCPConfig(type="json_store", enabled=True, config={
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
            MCPConfig(type="json_store", enabled=True,
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
            MCPConfig(type="json_store", enabled=True, config={"undo_depth": 2}))
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
            MCPConfig(type="json_store", enabled=True, config={"undo_depth": 0}))
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
            MCPConfig(type="json_store", enabled=True, config={
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
