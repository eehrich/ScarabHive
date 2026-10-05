"""A real agent, a real file_ops server and the real file_checkpoints plugin -- on tmp_path.

Shared by the plugin's own tests and by the tests of the surfaces (/chat
endpoints, agent-cli commands). Nothing on the recording path is faked: the
model is scripted, everything after it -- Agent.run_events, the tool hooks,
file_ops writing to disk, the plugin's record -- is the production code.
"""
from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_system.chat_actions import split_off_last_exchange
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    HooksConfig as AgentHooksConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    ToolConfig,
    ToolServerConfig,
)
from agent_system.file_rewind import unregister_file_rewinder
from agent_system.hooks import HooksConfig, HookType
from agent_system.hooks.registry import get_hook_registry
from agent_system.plugins.discovery import register_plugin_hooks
from agent_system.servers.agent.server import Agent
from agent_system.tools.base import ToolServerRegistry
from plugins.file_checkpoints.plugin import PLUGIN_FACTORY
from plugins.file_ops.server import FileOpsServer

USER = "alice"
SESSION = "rewind-s1"
BEFORE = "file_checkpoints.record_before_change"
AFTER = "file_checkpoints.record_after_change"

_ids = itertools.count(1)


def call(tool: str, **arguments: Any) -> Dict[str, Any]:
    """A tool call of the model on the rig's file server ``fs``."""
    return {"id": f"call_{next(_ids)}", "type": "function",
            "function": {"name": tool, "arguments": json.dumps(arguments)}}


def create(path: Path, content: str, overwrite: bool = False) -> Dict[str, Any]:
    return call("fs_manage", operation="create", path=str(path), content=content, overwrite=overwrite)


def delete(path: Path, recursive: bool = False) -> Dict[str, Any]:
    return call("fs_manage", operation="delete", path=str(path), recursive=recursive)


def move(path: Path, destination: Path) -> Dict[str, Any]:
    return call("fs_manage", operation="move", path=str(path), destination=str(destination))


def rename(path: Path, new_name: str) -> Dict[str, Any]:
    return call("fs_manage", operation="rename", path=str(path), new_name=new_name)


def replace(path: Path, old: str, new: str) -> Dict[str, Any]:
    return call("fs_replace_string_in_file", filePath=str(path), oldString=old, newString=new)


class ScriptedModel:
    """One round of tool calls per entry, then an answer."""

    model = "test/model"

    def __init__(self, *rounds: List[Dict[str, Any]], before_round: Optional[Dict[int, Any]] = None):
        self._rounds = rounds
        self.calls = 0
        self.results: List[Dict[str, Any]] = []
        #: round index (0-based) -> a function run before the model answers it -- a
        #: person at work while the turn runs
        self._before_round = before_round or {}

    def supports_streaming(self):
        return True

    async def chat_tools_streaming(self, messages, tools, cancellation_token=None, status_scope=None):
        for message in messages:
            if message.role == "tool" and isinstance(message.content, str):
                try:
                    self.results.append(json.loads(message.content))
                except ValueError:
                    pass
        if self.calls in self._before_round:
            self._before_round[self.calls]()
        self.calls += 1
        if self.calls <= len(self._rounds):
            yield {"type": "final", "assistant": {"role": "assistant", "content": None,
                                                  "tool_calls": self._rounds[self.calls - 1]}}
            return
        yield {"type": "final", "assistant": {"role": "assistant", "content": "done"}}


def tree(root: Path) -> Dict[str, Any]:
    """Every entry under ``root``: relative path -> bytes, "<dir>" or ("link", target)."""
    found: Dict[str, Any] = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(base) / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[key] = ("link", os.readlink(path))
            elif path.is_dir():
                found[key] = "<dir>"
            else:
                found[key] = path.read_bytes()
    return found


class Rig:
    """``work`` is the file server's only root, ``store`` the plugin's."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.work = tmp_path / "work"
        self.store = tmp_path / "store"
        self.work.mkdir()
        self.plugin = None
        self.registry = ToolServerRegistry()
        self.fs = self._file_server("fs", [self.work])
        self.registry.register("fs", self.fs)
        self.agents: Dict[str, Agent] = {}
        self._hook_names: List[str] = []
        self.request_ids: List[str] = []

    def _file_server(self, name: str, roots: List[Path]) -> FileOpsServer:
        config = ToolServerConfig(type="file_ops", enabled=True)
        config.allowed_directories = [str(root) for root in roots]
        config.search = {"enable_indexing": False,
                         "chroma_db_path": str(self.tmp_path / f"vector_{name}")}
        return FileOpsServer(name, AgentSystemConfig(), config)

    async def start(self, **config: Any):
        self.plugin = PLUGIN_FACTORY("file_checkpoints", AgentSystemConfig(), ToolServerConfig(
            type="file_checkpoints", enabled=True, config={"storage_path": str(self.store), **config}))
        self._hook_names = await register_plugin_hooks(
            "file_checkpoints", self.plugin, self.plugin.get_schema_data(), get_hook_registry(), HooksConfig())
        assert sorted(self._hook_names) == [AFTER, BEFORE], self._hook_names
        return self

    def agent(self, name: str = "coder", hooks: Optional[Dict[str, Any]] = None,
              allowed: Optional[List[str]] = None, max_steps: int = 12) -> Agent:
        """An agent on the rig's registry; the checkpoint hooks on unless ``hooks`` says otherwise."""
        if name in self.agents:
            return self.agents[name]
        llm_system = LLMSystemConfig(
            models={"gpt-4": LLMModelConfig(provider="openai", model="gpt-4", api_key="fake-key")},
            profiles={"normal": LLMProfile(model_ref="gpt-4")}, default_profile="normal")
        overrides = hooks if hooks is not None else {BEFORE: {"enabled": True}, AFTER: {"enabled": True}}
        agent_config = AgentConfig(max_steps=max_steps, llm_profile="normal",
                                   tools=ToolConfig(allowed=allowed or ["fs/*"]),
                                   hooks=AgentHooksConfig(overrides=overrides))
        agent = Agent(name, AgentSystemConfig(llm_system=llm_system),
                      ToolServerConfig(type="agent", enabled=True, agent_config=agent_config), self.registry)
        self.agents[name] = agent
        return agent

    async def turn(self, question: str, *rounds: List[Dict[str, Any]], agent: Optional[Agent] = None,
                   session_id: str = SESSION, user: str = USER, request_id: Optional[str] = None,
                   before_round: Optional[Dict[int, Any]] = None) -> ScriptedModel:
        """One turn of the conversation: the question, the model's rounds of calls, its answer."""
        agent = agent or self.agent()
        tracker = agent._session_tracker
        if not tracker.get_session_metadata(session_id):
            # What the API and agent-cli record before a run's first step.
            tracker.set_session_metadata(session_id, {"user_id": user, "agent_name": agent.name})
        model = ScriptedModel(*rounds, before_round=before_round)
        agent.llm = model
        request_id = request_id or f"run{next(_ids)}"
        self.request_ids.append(request_id)
        async for _ in agent.run_events(question, request_id=request_id, session_id=session_id):
            pass
        return model

    def messages(self, agent: Optional[Agent] = None, session_id: str = SESSION) -> list:
        return list((agent or self.agent())._session_tracker.get_session_messages(session_id))

    def undo_conversation(self, agent: Optional[Agent] = None, session_id: str = SESSION):
        """What /undo does to the conversation: the last exchange out, the files left alone."""
        agent = agent or self.agent()
        kept, dropped = split_off_last_exchange(self.messages(agent, session_id))
        assert dropped is not None, "nothing to undo"
        agent._session_tracker.set_session_messages(session_id, kept)
        return dropped

    async def rewind(self, checkpoint: Optional[int] = None, overwrite: bool = False, user: str = USER,
                     session_id: str = SESSION, agent: Optional[Agent] = None, registry: Any = None) -> dict:
        return await self.plugin.rewind(
            user_id=user, session_id=session_id, messages=self.messages(agent, session_id),
            checkpoint=checkpoint, registry=registry if registry is not None else self.registry,
            overwrite=overwrite)

    async def checkpoints(self, user: str = USER, session_id: str = SESSION,
                          agent: Optional[Agent] = None) -> dict:
        return await self.plugin.checkpoints(user_id=user, session_id=session_id,
                                             messages=self.messages(agent, session_id))

    async def close(self):
        registry = get_hook_registry()
        for name in self._hook_names:
            await registry.unregister_hook(HookType.PRE_TOOL_CALL, name)
            await registry.unregister_hook(HookType.POST_TOOL_CALL, name)
        if self.plugin is not None:
            unregister_file_rewinder(self.plugin)
        await self.fs.search_engine.stop()
