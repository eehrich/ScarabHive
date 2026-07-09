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
    async def test_write_requires_doc_and_payload(self, server):
        assert (await server.write({**SID, "data": {"a": 1}}))["status"] == "error"
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
