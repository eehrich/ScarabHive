"""Tests für die Reasoning-Artefakt-Invalidierung bei History-Mutation.

Invariante (utils/reasoning_artifacts.py): reasoning_details sind
integritäts-geschützte Artefakte über die exakte History. Jede Mutation
(Compaction, Summarization, Eviction) macht sie ungültig — der Helper
strippt alle außer der letzten Assistant-Message und flaggt diese als
`_rd_orphaned`, damit der LLM-Client (mode-bewusst) den Kettenreset
vollenden kann.
"""
from __future__ import annotations

from agent_system.utils.reasoning_artifacts import (
    RD_ORPHANED_FLAG,
    invalidate_reasoning_artifacts,
    strip_all_reasoning_artifacts,
    strip_reasoning_artifacts_containing,
)


def _msgs():
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}],
         "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_1", "data": "X"}]},
        {"role": "tool", "tool_call_id": "a", "content": "r1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}],
         "reasoning_details": [{"type": "reasoning.encrypted", "id": "rs_2", "data": "Y"}]},
        {"role": "tool", "tool_call_id": "b", "content": "r2"},
    ]


class TestInvalidateReasoningArtifacts:
    def test_strips_older_flags_latest(self):
        msgs = _msgs()
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2  # eine gestrippt + eine geflaggt
        assert "reasoning_details" not in msgs[1]          # ältere: gestrippt
        assert "reasoning_details" in msgs[3]              # letzte: behalten
        assert msgs[3][RD_ORPHANED_FLAG] is True           # aber geflaggt

    def test_noop_without_artifacts(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "answer"},
        ]
        assert invalidate_reasoning_artifacts(msgs) == 0
        assert RD_ORPHANED_FLAG not in msgs[1]

    def test_noop_without_assistant(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert invalidate_reasoning_artifacts(msgs) == 0

    def test_single_assistant_only_flagged(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "",
             "reasoning_details": [{"type": "reasoning.encrypted", "data": "X"}]},
        ]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 1
        assert "reasoning_details" in msgs[1]
        assert msgs[1][RD_ORPHANED_FLAG] is True

    def test_idempotent(self):
        msgs = _msgs()
        invalidate_reasoning_artifacts(msgs)
        # Zweiter Aufruf: ältere sind schon weg, letzte schon geflaggt
        n2 = invalidate_reasoning_artifacts(msgs)
        assert n2 == 1  # letzte wird erneut geflaggt (harmlos), nichts sonst
        assert "reasoning_details" in msgs[3]

    def test_object_messages(self):
        class M:
            def __init__(self, role, rd=None):
                self.role = role
                if rd is not None:
                    self.reasoning_details = rd

        older = M("assistant", rd=[{"data": "X"}])
        latest = M("assistant", rd=[{"data": "Y"}])
        msgs = [M("user"), older, M("tool"), latest]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2
        assert getattr(older, "reasoning_details", None) is None
        assert latest.reasoning_details == [{"data": "Y"}]
        assert getattr(latest, RD_ORPHANED_FLAG) is True

    def test_strip_containing_targets_only_named_item(self):
        """Chirurgische Heilung: nur die Message mit dem defekten Item verliert
        ihre Artefakte (OpenRouter-Bridge-Bug bei parallelen tool_calls —
        alle anderen Items der Kette verifizieren weiter)."""
        msgs = _msgs()
        n = strip_reasoning_artifacts_containing(msgs, "rs_1")
        assert n == 1
        assert "reasoning_details" not in msgs[1]
        assert "reasoning_details" in msgs[3]
        # unbekannte id: no-op
        assert strip_reasoning_artifacts_containing(msgs, "rs_zzz") == 0

    def test_strip_containing_pydantic_objects(self):
        from agent_system.llm.models import ChatMessage

        bad = ChatMessage(role="assistant", content="",
                          reasoning_details=[{"type": "reasoning.encrypted", "id": "rs_bad", "data": "X"}])
        good = ChatMessage(role="assistant", content="",
                           reasoning_details=[{"type": "reasoning.encrypted", "id": "rs_ok", "data": "Y"}])
        n = strip_reasoning_artifacts_containing([bad, good], "rs_bad")
        assert n == 1
        assert bad.reasoning_details is None
        assert good.reasoning_details == [{"type": "reasoning.encrypted", "id": "rs_ok", "data": "Y"}]

    def test_strip_all(self):
        msgs = _msgs()
        msgs[3][RD_ORPHANED_FLAG] = True
        n = strip_all_reasoning_artifacts(msgs)
        assert n == 2
        assert all("reasoning_details" not in m for m in msgs if m.get("role") == "assistant")
        assert RD_ORPHANED_FLAG not in msgs[3]

    def test_pydantic_chatmessage_objects(self):
        """Der Summarizer arbeitet auf echten ChatMessage-Objekten (pydantic):
        Felder sind nicht löschbar (delattr verboten) — der Helper muss auf
        None setzen; rd_orphaned ist deklariertes Feld und wird gesetzt."""
        from agent_system.llm.models import ChatMessage

        older = ChatMessage(role="assistant", content="",
                            reasoning_details=[{"type": "reasoning.encrypted", "data": "X"}])
        latest = ChatMessage(role="assistant", content="",
                             reasoning_details=[{"type": "reasoning.encrypted", "data": "Y"}])
        msgs = [ChatMessage(role="user", content="hi"), older,
                ChatMessage(role="tool", content="r", tool_call_id="a"), latest]
        n = invalidate_reasoning_artifacts(msgs)
        assert n == 2
        assert older.reasoning_details is None
        assert latest.reasoning_details == [{"type": "reasoning.encrypted", "data": "Y"}]
        assert latest.rd_orphaned is True
        # Serialisierung: Flag kommt bei model_dump(exclude_none=True) mit,
        # damit der LLM-Client es nach der Session-Roundtrip noch sieht
        dumped = latest.model_dump(exclude_none=True)
        assert dumped.get("rd_orphaned") is True
        assert older.model_dump(exclude_none=True).get("rd_orphaned") is None
