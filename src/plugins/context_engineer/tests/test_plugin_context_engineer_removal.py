"""Was passiert, wenn Nachrichten die Konversation verlassen.

Zwei Fragen, die vorher an verschiedenen Stellen verschieden beantwortet
wurden und jetzt durch einen Pfad laufen (``_archive_then_remove``):

1. Ist der Inhalt danach noch erreichbar? Pre-Layer P archivierte vorher,
   Layer 3 loeschte roh — dieselbe Operation, zwei Antworten.
2. Wie oft passiert es? Jede Entfernung schreibt den Anfang der Konversation
   um und entwertet damit den Provider-Prompt-Cache fuer alles dahinter.
"""

from __future__ import annotations

import json

import pytest

from plugins.context_engineer.archival_memory import ArchivalMemory
from plugins.context_engineer.compaction import (
    CompactionConfig,
    LayeredCompactionStrategy,
)
from plugins.context_engineer.core_memory import CoreMemory
from plugins.context_engineer.tool_result_store import ToolResultStore


def _turns(n: int) -> list[dict]:
    msgs: list[dict] = []
    for i in range(n):
        msgs.append({"role": "user", "content": f"Frage {i}"})
        msgs.append({"role": "assistant", "content": f"unersetzliche Antwort {i}"})
    return msgs


class TestPruneHysteresis:
    """Ein Prune muss ruhige Schritte kaufen, nicht beim naechsten neu ausloesen.

    Genau auf ``max_messages`` zu kuerzen legte den naechsten Schritt wieder
    darueber. Eine Session am Limit zahlte den Cache-Bruch also bei JEDEM
    Schritt fuer eine einzige gesparte Nachricht — der Grund, aus dem
    ``max_messages`` produktiv abgeschaltet war ("breaks cache").
    """

    @staticmethod
    def _strategy(tmp_path, headroom: int):
        return LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9,
                max_messages=40, max_messages_headroom=headroom,
                keep_system_messages=True,
            ),
        )

    @staticmethod
    def _over_the_limit() -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "die Aufgabe"}]
        msgs += [{"role": "assistant", "content": f"Antwort {i}"} for i in range(44)]
        msgs += [{"role": "user", "content": "letzte Frage"}]
        return msgs

    @pytest.mark.asyncio
    async def test_a_prune_goes_below_the_limit_not_to_it(self, tmp_path):
        strat = self._strategy(tmp_path, headroom=20)

        result = await strat.compact(self._over_the_limit(), current_tokens=100)

        kept = len(result.modified_messages)
        assert kept <= 40 - 20 + 1, (
            f"nur auf {kept} gekuerzt; ein Prune, der AUF dem Limit landet, "
            f"loest bei der naechsten Nachricht sofort wieder aus")

    @pytest.mark.asyncio
    async def test_the_next_steps_are_quiet(self, tmp_path):
        """Der Gewinn: ein Cache-Bruch statt einer pro Schritt."""
        strat = self._strategy(tmp_path, headroom=20)
        messages = self._over_the_limit()

        pruning_rounds = 0
        for step in range(15):
            result = await strat.compact(messages, current_tokens=100)
            if result.messages_pruned:
                pruning_rounds += 1
            # compact() arbeitet auf einer Kopie; die Konversation geht mit dem
            # Ergebnis weiter, so wie der Hook sie auch zurueckschreibt.
            messages = result.modified_messages
            messages.append({"role": "assistant", "content": f"neu {step}"})
            assert len(messages) <= 41, (
                f"Schritt {step}: {len(messages)} Nachrichten liegen ueber dem "
                f"Limit — die Hysterese darf die Liste nicht wachsen lassen")

        assert pruning_rounds == 1, (
            f"{pruning_rounds} von 15 Schritten haben geprunt. Jeder schreibt "
            f"den Anfang der Konversation um und entwertet den Prompt-Cache "
            f"dahinter; genau dafuer gibt es den Headroom")

    @pytest.mark.asyncio
    async def test_headroom_zero_still_trims_to_the_limit(self, tmp_path):
        """Das alte Verhalten bleibt erreichbar — manche Agents wollen eine harte Decke."""
        strat = self._strategy(tmp_path, headroom=0)

        result = await strat.compact(self._over_the_limit(), current_tokens=100)
        assert 39 <= len(result.modified_messages) <= 40


class TestLayer3DoesNotDestroy:
    """Layer 3 archiviert vor dem Loeschen, wie Pre-Layer P direkt daneben.

    Es war die letzte Stelle, die Nachrichten ohne Kopie entfernte. Der
    Docstring nannte das "irreversible", und in den letzten 1000 produktiven
    Kompaktionen feuerte die Schicht null Mal — die Luecke war also latent,
    nicht beobachtet. Eine Luecke bleibt sie: die Schicht laeuft, sobald die
    Token-Schwellen es sagen.
    """

    @staticmethod
    def _strategy(tmp_path, keep_system: bool = True):
        archival = ArchivalMemory(tmp_path / "archive.db", session_id="t")
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=archival,
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=1, target_tokens=0,
                drop_after_turns=2, max_messages=0,
                keep_system_messages=keep_system,
            ),
        )
        return strat, archival

    @pytest.mark.asyncio
    async def test_dropped_messages_reach_the_archive(self, tmp_path):
        strat, archival = self._strategy(tmp_path)
        messages = _turns(8)

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped > 0, (
            "Layer 3 hat nichts verworfen — diese Fixture prueft die Schicht nicht")
        stored = [r[0] for r in archival._db.execute(
            "SELECT content FROM archived_messages").fetchall()]
        live = {str(m.get("content")) for m in result.modified_messages}
        for i in range(3):
            text = f"unersetzliche Antwort {i}"
            assert any(text in s for s in stored) or text in live, (
                f"'{text}' hat die Konversation verlassen, ohne im Archiv anzukommen")

    @pytest.mark.asyncio
    async def test_a_failed_archive_write_keeps_the_messages(self, tmp_path, monkeypatch):
        """Nicht loeschen schlaegt ohne Kopie loeschen.

        Dieselbe Abwaegung wie bei Pre-Layer P: ein zu langer Kontext kostet,
        zerstoerter Inhalt ist nicht wiederherstellbar. Der Schreibvorgang ist
        alles-oder-nichts, ein Fehlschlag betrifft also den ganzen Stapel.
        """
        strat, archival = self._strategy(tmp_path)

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(archival, "store_many", boom)

        messages = _turns(8)
        before = [str(m.get("content")) for m in messages]

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        assert result.messages_dropped == 0
        after = [str(m.get("content")) for m in result.modified_messages]
        assert before == after, "Nachrichten wurden zerstoert, obwohl nichts gespeichert wurde"

    @pytest.mark.asyncio
    async def test_the_breadcrumb_survives_a_drop(self, tmp_path):
        """Der Hinweis ist ueber seinen TYP geschuetzt, nicht ueber die Rolle.

        Layer 3 pruefte nur die Rolle. Mit ``keep_system_messages=False`` warf
        sie damit die einzige Nachricht raus, die dem Agent sagt, dass ueberhaupt
        etwas ausgelagert wurde — und der Laufzaehler darin startet beim
        naechsten Prune wieder bei null.
        """
        strat, _ = self._strategy(tmp_path, keep_system=False)
        notice = json.dumps({"type": "pruned_notice", "total_removed": 137,
                             "hint": "aeltere Nachrichten wurden ausgelagert"})
        messages = [{"role": "system", "content": notice}] + _turns(8)

        result = await strat.compact(messages, current_tokens=10_000, force=True)

        kept = [m for m in result.modified_messages
                if "pruned_notice" in str(m.get("content"))]
        assert kept, "der Hinweis wurde verworfen — der Agent kann nicht wissen, dass es etwas zu holen gibt"
        assert json.loads(kept[0]["content"])["total_removed"] >= 137, (
            "der Laufzaehler wurde zurueckgesetzt")


class TestLayer1ResultsAreFindable:
    """A stored result the agent cannot LIST is only half stored.

    Layer 1 called store_and_reference without a session_id, so every result
    landed under "default" while list/search query the real id. Measured: one
    compaction, then list(section='tool_results') answered "0 of 0" - and the
    system prompt tells the model to look exactly there. read(ref=...) still
    worked, which is why nothing noticed: the catalogue was blind, not the
    content. The tests that covered list() seeded the store by hand with the
    right id and so never exercised the path production uses.
    """

    @pytest.mark.asyncio
    async def test_a_compacted_result_shows_up_in_the_catalogue(self, tmp_path):
        from pathlib import Path

        from plugins.context_engineer.hooks import ContextEngineerPlugin

        hooks = ContextEngineerPlugin(Path("src/plugins/context_engineer"))
        hooks._storage_base = tmp_path
        sid = "a-real-session"
        strat = hooks._get_session_components(sid)["strategy"]
        strat.config.layer1_threshold = 1
        strat.config.tool_result_min_size = 20
        strat.config.tool_result_keep_last = 0

        messages = [
            {"role": "user", "content": "mach was"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "name": "t", "tool_call_id": "call_1",
             "content": "sehr grosses Werkzeug-Ergebnis " * 80},
        ]
        result = await strat.compact(
            messages, current_tokens=10**6, force=True, session_id=sid)
        assert result.tool_results_stored == 1, "fixture stored nothing"

        listed = await hooks._handle_context_list(
            section="tool_results", session_id=sid)
        assert listed["total"] == 1, (
            "the compacted result is not in the catalogue - list() is blind "
            "while the system prompt points the model at it")
        assert "Werkzeug-Ergebnis" in listed["entries"][0]["summary"], (
            "the preview never reaches the only place that shows it")

        found = await hooks._handle_context_list(
            filter="Werkzeug", session_id=sid)
        assert found["count"] >= 1, "search cannot reach it either"


class TestHeadroomNeverCollapsesTheConversation:
    """The headroom must trim, not empty.

    Unclamped, max(1, max_messages - headroom) went to 1 for ANY limit below
    the default headroom of 50: an agent configured with max_messages=40 lost
    its whole conversation on the first prune instead of 20 messages.
    """

    @pytest.mark.parametrize("max_messages", [200, 40, 30, 10, 2])
    @pytest.mark.asyncio
    async def test_a_prune_keeps_a_workable_conversation(self, tmp_path, max_messages):
        strat = LayeredCompactionStrategy(
            tool_store=ToolResultStore(tmp_path / "tools.db"),
            core_memory=CoreMemory(storage_path=tmp_path / "memory.json"),
            archival_memory=ArchivalMemory(tmp_path / "archive.db", session_id="t"),
            config=CompactionConfig(
                layer1_threshold=10**9, layer2_threshold=10**9,
                layer3_threshold=10**9,
                max_messages=max_messages, max_messages_headroom=50,
                keep_system_messages=True,
            ),
        )
        messages = [{"role": "user", "content": "die Aufgabe"}]
        messages += [{"role": "assistant", "content": f"Antwort {i}"}
                     for i in range(max_messages * 2)]
        messages += [{"role": "user", "content": "letzte Frage"}]

        result = await strat.compact(messages, current_tokens=100)
        kept = len(result.modified_messages)

        # The floor is 3 and cannot be crossed: the first user message (the
        # task), the last one (or the request is invalid) and the breadcrumb
        # are all protected. A limit below that is a nonsense config, and
        # Pre-Layer P already logs a warning when it cannot reach it.
        assert kept <= max(max_messages, 3), f"still over the limit: {kept}"
        # Half the limit is the deepest a prune may ever go.
        assert kept >= min(max_messages // 2, max_messages), (
            f"max_messages={max_messages} collapsed to {kept} messages - the "
            f"headroom cut deeper than the limit itself")
