"""Sub-agents must see the parent's CURRENT context_vars when continued.

A sub-agent inherits context_vars once, at session-create time. Continuing it
used to restore only that frozen snapshot, so after the coordinator called
set_context(aufgabe=World) and continued an existing writer, the writer's
prompt rendered `{{ aufgabe }}` as the PREVIOUS task while the task text said
"World" — two contradicting instructions in one prompt.

merge_parent_context_vars() refreshes from the parent's live tracker while
preserving vars the sub-agent set itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.sub_agent_manager.manager import SubAgentManager


merge = SubAgentManager.merge_parent_context_vars


class TestMergeRule:
    def test_parent_update_reaches_untouched_key(self):
        # The reported bug: coordinator moved on to the next task.
        sub = {"context_vars": {"aufgabe": "Idee", "book_id": 5},
               "context_vars_inherited": {"aufgabe": "Idee", "book_id": 5}}
        merged, inherited = merge(sub, {"aufgabe": "World", "book_id": 5})
        assert merged["aufgabe"] == "World"
        assert merged["book_id"] == 5
        assert inherited == {"aufgabe": "World", "book_id": 5}

    def test_sub_agent_own_value_survives_parent_update(self):
        # sub_agent_audio_reworker & friends may call set_context themselves.
        sub = {"context_vars": {"phase": "rework", "book_id": 5},
               "context_vars_inherited": {"phase": "init", "book_id": 5}}
        merged, _ = merge(sub, {"phase": "init", "book_id": 7})
        assert merged["phase"] == "rework"   # self-set wins
        assert merged["book_id"] == 7        # untouched key follows the parent

    def test_sub_only_key_is_kept(self):
        sub = {"context_vars": {"scene_id": 3},
               "context_vars_inherited": {}}
        merged, _ = merge(sub, {"book_id": 5})
        assert merged == {"scene_id": 3, "book_id": 5}

    def test_legacy_session_without_provenance_follows_parent(self):
        # No context_vars_inherited key -> assume everything was inherited,
        # so the parent's current values get through (that is the fix).
        sub = {"context_vars": {"aufgabe": "Idee"}}
        merged, _ = merge(sub, {"aufgabe": "World"})
        assert merged["aufgabe"] == "World"

    def test_empty_parent_leaves_sub_untouched(self):
        # Provenance must SURVIVE an empty parent — erasing it would make every
        # sub var look self-set, permanently blocking future parent updates.
        sub = {"context_vars": {"aufgabe": "Idee"},
               "context_vars_inherited": {"aufgabe": "Idee"}}
        merged, inherited = merge(sub, {})
        assert merged == {"aufgabe": "Idee"}
        assert inherited == {"aufgabe": "Idee"}

    def test_none_parent_vars(self):
        merged, inherited = merge({"context_vars": {"a": 1}}, None)
        assert merged == {"a": 1} and inherited == {"a": 1}

    def test_parent_can_still_update_after_an_empty_read(self):
        """An empty/failed parent read must not poison later refreshes."""
        sub = {"context_vars": {"aufgabe": "Idee"},
               "context_vars_inherited": {"aufgabe": "Idee"}}
        merged, inherited = merge(sub, {})          # e.g. tracker unreadable
        sub = {"context_vars": merged, "context_vars_inherited": inherited}
        merged2, _ = merge(sub, {"aufgabe": "World"})
        assert merged2["aufgabe"] == "World"

    def test_repeated_refresh_is_stable(self):
        """Self-set values must not be re-captured as inherited on the next pass."""
        sub = {"context_vars": {"phase": "rework", "aufgabe": "Idee"},
               "context_vars_inherited": {"phase": "init", "aufgabe": "Idee"}}
        parent = {"phase": "init", "aufgabe": "World"}

        merged, inherited = merge(sub, parent)
        sub = {"context_vars": merged, "context_vars_inherited": inherited}
        assert merged["phase"] == "rework" and merged["aufgabe"] == "World"

        # Second continue, parent unchanged: self-set value still survives.
        merged2, inherited2 = merge(sub, parent)
        assert merged2["phase"] == "rework"
        assert merged2["aufgabe"] == "World"

        # Third continue, parent moves on again.
        sub = {"context_vars": merged2, "context_vars_inherited": inherited2}
        merged3, _ = merge(sub, {"phase": "init", "aufgabe": "Charakter"})
        assert merged3["phase"] == "rework"
        assert merged3["aufgabe"] == "Charakter"


class TestRefreshPersists:
    def _manager(self):
        session_service = MagicMock()
        session_service.session_manager.save_session = AsyncMock()
        mgr = SubAgentManager.__new__(SubAgentManager)   # skip __init__ wiring
        mgr._session_service = session_service
        return mgr, session_service

    def _parent(self, vars_):
        parent = MagicMock()
        parent._session_tracker.get_session_template_vars.return_value = vars_
        return parent

    @pytest.mark.asyncio
    async def test_refresh_updates_and_saves(self):
        mgr, svc = self._manager()
        sub_data = {"session_id": "sub-1",
                    "context_vars": {"aufgabe": "Idee"},
                    "context_vars_inherited": {"aufgabe": "Idee"}}
        merged = await mgr.refresh_sub_context_vars(
            user_id="u", sub_session_id="sub-1", sub_session_data=sub_data,
            parent_agent=self._parent({"aufgabe": "World"}),
            parent_session_id="parent")

        assert merged["aufgabe"] == "World"
        assert sub_data["context_vars"]["aufgabe"] == "World"
        assert sub_data["context_vars_inherited"] == {"aufgabe": "World"}
        svc.session_manager.save_session.assert_awaited_once_with(sub_data)

    @pytest.mark.asyncio
    async def test_no_change_means_no_write(self):
        mgr, svc = self._manager()
        sub_data = {"context_vars": {"aufgabe": "Idee"},
                    "context_vars_inherited": {"aufgabe": "Idee"}}
        await mgr.refresh_sub_context_vars(
            user_id="u", sub_session_id="sub-1", sub_session_data=sub_data,
            parent_agent=self._parent({"aufgabe": "Idee"}),
            parent_session_id="parent")
        svc.session_manager.save_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_parent_tracker_does_not_break_continue(self):
        mgr, svc = self._manager()
        parent = MagicMock()
        parent._session_tracker.get_session_template_vars.side_effect = RuntimeError("boom")
        sub_data = {"context_vars": {"aufgabe": "Idee"},
                    "context_vars_inherited": {"aufgabe": "Idee"}}
        merged = await mgr.refresh_sub_context_vars(
            user_id="u", sub_session_id="sub-1", sub_session_data=sub_data,
            parent_agent=parent, parent_session_id="parent")
        assert merged == {"aufgabe": "Idee"}   # falls back to the snapshot
        svc.session_manager.save_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_parent_agent(self):
        mgr, _ = self._manager()
        sub_data = {"context_vars": {"a": 1}, "context_vars_inherited": {"a": 1}}
        merged = await mgr.refresh_sub_context_vars(
            user_id="u", sub_session_id="sub-1", sub_session_data=sub_data,
            parent_agent=None, parent_session_id="parent")
        assert merged == {"a": 1}
