"""Web endpoints of the SSH control plugin: the SSH Machines panel and the calls it makes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import asyncssh
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

HISTORY_SHOWN = 50


class RunCommand(BaseModel):
    machine: str
    command: str


class NewMachine(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    auth_method: Literal["key", "password", "agent"] = "key"
    password: str | None = None
    key_path: str = "~/.ssh/id_rsa"
    persistent: bool = False


class SSHControlWebEndpoints:
    """The panel works on the tool server's connection manager and history, and adds and removes machines through its tools."""

    def __init__(self, server):
        self.server = server
        self.templates = ui_templates(Path(__file__).parent / "templates")

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.server.name, schema=self.server.get_schema_data(), handler_class=self)

    def get_static_assets(self) -> Path:
        return Path(__file__).parent / "static"

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(request, "panel.html", {"plugin": self.server.name})

    async def list_machines(self, request: Request, active: bool = False) -> dict:
        """Each machine with its connection state -- never a credential. `active` pings the machines connected to
        before instead of judging by their last use; one never connected to is not connected to for it."""
        manager = self.server.connection_manager
        machines = list(manager.machines.values())

        async def state(name: str) -> dict:
            # a machine without a free slot is not waited for, its last use says enough -- decided in the step that
            # takes the slot, or a command started in between would make the ping wait for it
            pool = manager.pools.get(name)
            return await manager.check_connection(name, lazy=not active or (pool is not None and not pool.has_free_slot()))

        states = await asyncio.gather(*(state(machine.name) for machine in machines))
        last_run = {entry["machine"]: entry["timestamp"] for entry in self.server.command_history}
        return {"machines": [{
            "name": machine.name, "host": machine.host, "port": machine.port, "username": machine.username,
            "tags": machine.tags, "connected": state["connected"], "latency_ms": state["latency_ms"],
            "error": state["error"], "not_yet_connected": state["not_yet_connected"],
            "last_run": last_run.get(machine.name),  # changes with every run recorded: the panel loads the history anew
        } for machine, state in zip(machines, states)]}

    async def machine_history(self, request: Request, machine_name: str) -> dict:
        """The last runs on the machine, oldest first: the agents' and the panel's, failed ones too."""
        runs = [entry for entry in self.server.command_history if entry["machine"] == machine_name]
        return {"history": runs[-HISTORY_SHOWN:]}

    async def run_command(self, request: Request, run: RunCommand) -> dict:
        """A machine that cannot be reached or a command that does not finish in time is an error here."""
        manager = self.server.connection_manager
        if run.machine not in manager.machines:
            raise HTTPException(status_code=404, detail=f"Machine '{run.machine}' not found")
        try:
            result = await manager.execute_command(run.machine, run.command)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=f"No answer from {run.machine} in time") from exc
        except (OSError, ValueError, asyncssh.Error) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.model_dump()

    async def add_machine(self, request: Request, machine: NewMachine) -> dict:
        """Through the tool: tested before it is added, stored only if it can be restored. Its refusal is an error here."""
        result = await self.server.add_machine(machine.model_dump())
        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    async def remove_machine(self, request: Request, name: str) -> dict:
        """For good, out of the store too. One from the configuration comes back at the next start (`config_error`)."""
        if name not in self.server.connection_manager.machines:
            raise HTTPException(status_code=404, detail=f"Machine '{name}' not found")
        result = await self.server.remove_machine({"name": name, "remove_from_config": True})
        if not result["success"]:
            raise HTTPException(status_code=500, detail=result["error"])
        return result
