"""The Agent Editor API: admin only, JSON in and out. The store does the file work, sources the picker data."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from agent_system.auth.database import get_db
from agent_system.auth.dependencies import bearer_scheme, get_token_user
from agent_system.auth.models import UserRole
from agent_system.plugins.schema_router import create_schema_router
from agent_system.ui.resources import ui_templates

from . import sources
from plugins.sub_agent_manager.server import agent_allowed

from .store import (MANAGER_TYPE, PROMPT_SUFFIXES, Snapshot, Store, StoreError, base_of, catalog_for, dump, error_text,
                    hide_environment, jsonable, manager_lists, parent_of, placeholders, quiet_loader, resolve_with,
                    to_node, unplain)

logger = logging.getLogger(__name__)

PROMPT_LIMIT = 200_000
#: A name no server can have (the name rule forbids a leading underscore), for resolving an unsaved entry.
UNSAVED = "_agent_editor_unsaved"


def group_of(rel: str) -> str:
    """`src/plugins*/<plugin>/…` belongs to that plugin, everything else to the config."""
    parts = rel.split("/")
    return parts[2] if len(parts) > 3 and parts[0] == "src" and parts[1].startswith("plugins") else "config"


def unsafe_path(path: str) -> bool:
    """Absolute, UNC, drive and stream paths, refused as text: on Windows even resolving `//host/share` connects."""
    flat = path.replace("\\", "/")
    return not flat or "\x00" in flat or ":" in flat or flat.startswith("/") or "//" in flat


def _typed_entry(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> str:
    """`- !web/*` typed without quotes: a YAML tag to the parser, the removal entry `!web/*` to whoever typed it.

    Only a tag alone on its node: in a flow list the tag swallows the comma and what follows (`[!a/*, +b/*]` is the tag
    `!a/*,` on `+b/*`), and a tag before a value is no entry either -- both are refused with the way out. So is a `]`
    without its `[`: the tag takes a flow list's closing bracket too (`[!a/*] ]`), a glob's `[ab]` stays.
    """
    swallowed = "," in suffix or suffix.count("]") > suffix.count("[")
    if not isinstance(node, yaml.ScalarNode) or node.value or swallowed:
        raise yaml.constructor.ConstructorError(
            None, None, f"the tag !{suffix} is not supported here: write an entry that starts with ! in quotes, "
            "e.g. '!web/*'", node.start_mark)
    return f"!{suffix}"


class TypedLoader(yaml.SafeLoader):
    """The parser for YAML typed into the panel: SafeLoader, plus unquoted `!entry` scalars (written back quoted)."""


TypedLoader.add_multi_constructor("!", _typed_entry)


def has_anchors(text: str) -> bool:
    # the pure parser, as TypedLoader: libyaml refuses a glob's [..] in a tag (`- !web/[ab]*`)
    return any(isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None)
               for event in yaml.parse(text, Loader=TypedLoader))


def _quietly(function: Callable, *args: Any) -> Any:
    with quiet_loader():
        return function(*args)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=422, detail=f"{field}: a non-empty string is required")
    return value


def _optional_text(value: Any, field: str) -> Optional[str]:
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{field}: a string or null is required")
    return value or None


def _mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{field}: an object is required")
    return value


def _string_list(value: Any, field: str) -> Optional[list[str]]:
    if value is not None and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
        raise HTTPException(status_code=422, detail=f"{field}: a list of strings or null is required")
    return value


class AgentEditorWebEndpoints:
    def __init__(self, plugin):
        self.plugin = plugin
        self.templates = ui_templates(Path(__file__).parent / "templates")
        self._stores: dict[tuple[str, str], Store] = {}

    def get_web_router(self) -> APIRouter:
        return create_schema_router(plugin_name=self.plugin.name, schema=self.plugin.get_schema_data(), handler_class=self)

    async def render_panel(self, request: Request):
        return self.templates.TemplateResponse(
            request, "panel.html", {"plugin": self.plugin.name, "auth_enabled": self.plugin.auth_enabled})

    # ------------------------------------------------------------------ plumbing

    async def _admin(self, request: Request) -> None:
        """An active admin, checked against the database here, whatever the route rules in the config say."""
        if not self.plugin.auth_enabled:
            raise HTTPException(status_code=403, detail="The agent editor needs authentication to be enabled")
        user = await get_token_user(request, await bearer_scheme(request), get_db())
        if not user.is_active or user.role != UserRole.ADMIN:
            raise HTTPException(status_code=403, detail="Admin privileges required")

    async def _body(self, request: Request) -> dict:
        await self._admin(request)
        # A write without a JSON Content-Type is a simple cross-site request.
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="Send the request as JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="The body is not valid JSON")
        return _mapping(body, "body")

    def _store(self, request: Request) -> Store:
        """The config tree: the plugin's `config_path`/`root` if set, else the app's config and the working directory."""
        config = self.plugin.server_config
        config_path = (getattr(config, "config_path", None) or getattr(request.app.state, "config_path", None)
                       or "config/config.yaml")
        root = getattr(config, "root", None) or Path.cwd()
        key = (str(config_path), str(root))
        if key not in self._stores:
            self._stores[key] = Store(Path(config_path), Path(root))
        return self._stores[key]

    @staticmethod
    async def _run(function: Callable, *args: Any) -> Any:
        """Store and file work off the event loop (a config load takes a tenth of a second), errors as HTTP answers.

        The loader's report stays quiet in there (the app logs it at start and reload); an error shows no value of a
        variable the files or the request name.
        """
        try:
            return await run_in_threadpool(_quietly, function, *args)
        except StoreError as error:
            names = placeholders(args)
            # a write passes the store's bound method
            for arg in (getattr(function, "__self__", None), *args):
                if isinstance(arg, Store):
                    names |= arg.known_names()
            raise HTTPException(status_code=error.status, detail=hide_environment(str(error), names))

    # ------------------------------------------------------------------ agents

    async def list_agents(self, request: Request):
        await self._admin(request)
        try:
            tools: Optional[list[dict]] = await sources.tool_catalog(request.app.state)
            tools_error = None
        except Exception as error:  # the list stands without the tool check, and says so
            logger.warning("agent list: the tool catalogue failed: %s", error_text(error))
            tools, tools_error = None, error_text(error)
        return await self._run(self._rows, self._store(request), request.app.state, tools, tools_error)

    def _rows(self, store: Store, state: Any, tools: Optional[list[dict]], tools_error: Optional[str]) -> dict:
        snap = store.snapshot()  # one snapshot for every row: a write in between must not mix two states
        catalog = catalog_for(snap.config)
        is_agent = sources.is_agent_rule(state, catalog)
        skill_names = {skill["name"] for skill in sources.skills(snap.config)}
        external = sources.external_servers(snap.config)
        checked: dict[tuple, dict] = {}

        def check(allowed: list[str], blocked: list[str]) -> dict:
            key = (tuple(allowed), tuple(blocked))  # most agents share their parent's lists
            if key not in checked:
                checked[key] = sources.effective_tools(tools or [], allowed, blocked, external)
            return checked[key]

        rows = []
        for name in snap.config.plugins.servers:
            base = base_of(snap.config, snap.resolved.get(name), name)
            if is_agent(name, base):
                row = self._row(store, snap, state, catalog, name, base)
                row["problems"] = snap.mask(
                    sources.problems(store, snap, name, skill_names, None if tools is None else check))
                rows.append(row)
        runtime = getattr(state, "runtime", None)
        for name, decl in (runtime.declarations().items() if runtime is not None else ()):
            if name not in snap.config.plugins.servers and is_agent(name, decl.type):
                rows.append(self._removed_row(snap, name, decl))
        errors = snap.errors + [f"{name}: {error}" for name, error in snap.resolve_errors.items()]
        if tools_error:
            errors.append(f"Tool patterns were not checked, the tool catalogue failed: {tools_error}")
        return {"agents": sorted(rows, key=lambda row: row["name"]), "errors": snap.mask(errors)}

    @staticmethod
    def _row(store: Store, snap: Snapshot, state: Any, catalog: Any, name: str, base: str) -> dict:
        server = snap.config.plugins.servers[name]
        resolved = snap.resolved.get(name)
        shown = resolved or server
        metadata = shown.metadata
        files = [store.rel(path) for path in snap.defined.get(name, [])]
        reason = store.readonly_reason(name, snap) or store.form_reason(name, snap)
        live = sources.live_state(name, resolved, server.enabled, state)
        return {
            "name": name, "type": server.type, "base": base, "enabled": server.enabled,
            "description": snap.mask(shown.description),
            "visibility": sources.visibility(resolved, catalog) if resolved else "private",
            "category": metadata.category if metadata else None,
            "tags": (metadata.tags or []) if metadata else [],
            "group": group_of(files[0]) if files else "config",
            "file": files[0] if files else None, "files": files,
            "editable": reason is None, "readonly_reason": reason,
            "state": live["state"], "changed": live["changed"], "restart": live["restart"],
        }

    @staticmethod
    def _removed_row(snap: Snapshot, name: str, decl: Any) -> dict:
        metadata = decl.server_config.metadata
        return {
            "name": name, "type": decl.type, "base": decl.type, "enabled": False,
            "description": snap.mask(decl.server_config.description), "visibility": decl.visibility,
            "category": metadata.category if metadata else None, "tags": (metadata.tags or []) if metadata else [],
            "group": "config", "file": None, "files": [],
            "editable": False, "readonly_reason": "no longer in the config files",
            "state": "removed", "changed": [], "restart": True, "problems": [],
        }

    async def get_agent(self, request: Request, name: str):
        await self._admin(request)
        return await self._run(self._detail, self._store(request), request.app.state, name)

    def _detail(self, store: Store, state: Any, name: str) -> dict:
        snap = store.snapshot()
        is_agent = sources.is_agent_rule(state, catalog_for(snap.config))
        runtime = getattr(state, "runtime", None)
        if name not in snap.config.plugins.servers:
            decl = runtime.describe(name) if runtime is not None else None
            if decl is None or not is_agent(name, decl.type):
                raise StoreError(404, f"{name} is not an agent in the config")
            return {"name": name, "file": None, "files": [], "version": None, "editable": False,
                    "readonly_reason": "no longer in the config files", "form_reason": None, "own": None, "parent": None,
                    "inherited": None, "effective": store.present(decl.server_config.model_dump(mode="json"), snap),
                    "state": "removed", "changed": [], "reload_fields": [], "restart": True,
                    "children": [], "spawnable": [], "prompt": None}
        server = snap.config.plugins.servers[name]
        resolved = snap.resolved.get(name)
        if not is_agent(name, base_of(snap.config, resolved, name)):
            raise StoreError(404, f"{name} is not an agent")
        try:
            inherited = sources.inherited(snap, name, server.type if "type" in server.model_fields_set else None)
        except Exception as error:
            logger.info("%s: the inherited part does not resolve: %s", name, error_text(error))
            inherited = None
        reason = store.readonly_reason(name, snap)
        prompt = None
        agent = resolved.agent_config if resolved is not None else None
        if agent is not None and agent.system_template and not agent.system_prompt:
            target = store.root / agent.system_template
            prompt = {"path": store.rel(target), "exists": target.is_file()}
        return {
            "name": name,
            "file": store.rel(snap.defined[name][0]) if snap.defined.get(name) else None,
            "files": [store.rel(path) for path in snap.defined.get(name, [])],
            "version": store.version(name, snap), "editable": reason is None, "readonly_reason": reason,
            "form_reason": store.form_reason(name, snap),
            "own": jsonable(store.own(name, snap)),
            "parent": parent_of(snap.config, name),
            "inherited": store.present(inherited.model_dump(mode="json"), snap) if inherited else None,
            "effective": store.present(resolved.model_dump(mode="json"), snap) if resolved else None,
            **sources.live_state(name, resolved, server.enabled, state),
            "children": store.children(name, snap),
            "spawnable": snap.mask(store.spawnable(name, snap)),
            "prompt": prompt,
        }

    async def get_inherited(self, request: Request, typ: Optional[str] = Query(None, alias="type")):
        await self._admin(request)
        return await self._run(self._inherited, self._store(request), typ)

    @staticmethod
    def _inherited(store: Store, typ: Optional[str]) -> dict:
        snap = store.snapshot()
        known = set(snap.config.plugins.servers) | catalog_for(snap.config).types() | {"agent"}
        if typ and typ not in known:
            raise StoreError(404, f"{typ} is neither a server nor a plugin type")
        try:
            inherited = sources.inherited(snap, UNSAVED, typ)
        except Exception as error:
            raise StoreError(422, f"{typ} does not resolve: {error_text(error)}")
        if inherited is None:
            raise StoreError(404, f"{typ} is neither a server nor a plugin type")
        return {"inherited": store.present(inherited.model_dump(mode="json"), snap)}

    async def create_agent(self, request: Request):
        body = await self._body(request)
        name, entry = _text(body.get("name"), "name"), _mapping(body.get("entry"), "entry")
        source = _optional_text(body.get("source"), "source")
        result = await self._run(self._store(request).create, name, entry, source, bool(body.get("dry_run")))
        if not body.get("dry_run"):
            logger.info("Agent editor: created %s in %s", name, result["file"])
        return result

    async def save_agent(self, request: Request, name: str):
        body = await self._body(request)
        entry, version = _mapping(body.get("entry"), "entry"), _text(body.get("version"), "version")
        result = await self._run(self._store(request).save, name, entry, version, bool(body.get("dry_run")))
        if not body.get("dry_run") and result["diff"]:
            logger.info("Agent editor: saved %s", name)
        return result

    async def delete_agent(self, request: Request, name: str):
        body = await self._body(request)
        version = _text(body.get("version"), "version")
        result = await self._run(self._store(request).delete, name, version, bool(body.get("dry_run")))
        if not body.get("dry_run"):
            logger.info("Agent editor: deleted %s%s", name, " and its file" if result["deleted_file"] else "")
        return result

    async def set_spawnable(self, request: Request, name: str):
        body = await self._body(request)
        sam, version = _text(body.get("sam"), "sam"), _text(body.get("version"), "version")
        allowed = body.get("allowed")
        if not isinstance(allowed, bool):
            raise HTTPException(status_code=422, detail="allowed: true or false is required")
        result = await self._run(self._store(request).set_spawnable, name, sam, allowed, version, bool(body.get("dry_run")))
        if not body.get("dry_run") and result["diff"]:
            logger.info("Agent editor: %s may %sspawn %s", sam, "" if allowed else "no longer ", name)
        return result

    # ------------------------------------------------------------------ managers

    async def list_managers(self, request: Request):
        await self._admin(request)
        return await self._run(self._managers, self._store(request), request.app.state)

    @staticmethod
    def _managers(store: Store, state: Any) -> dict:
        """The enabled managers, and for each the agents its lists let it start (the manager's own rule)."""
        snap = store.snapshot()
        is_agent = sources.is_agent_rule(state, catalog_for(snap.config))
        agents = sorted(name for name in snap.config.plugins.servers
                        if is_agent(name, base_of(snap.config, snap.resolved.get(name), name)))
        rows = []
        for sam in store.sams(snap):
            allowed, blocked = manager_lists(snap.resolved[sam])
            files = snap.defined.get(sam, [])
            reason = store.readonly_reason(sam, snap) or store.form_reason(sam, snap)
            rows.append({
                "name": sam, "file": store.rel(files[0]) if files else None, "version": store.version(sam, snap),
                "editable": reason is None, "readonly_reason": reason,
                "own": jsonable(store.own(sam, snap)), "allowed": snap.mask(allowed), "blocked": snap.mask(blocked),
                "spawns": [name for name in agents if agent_allowed(name, allowed, blocked)],
            })
        return {"managers": rows, "agents": agents}

    async def create_manager(self, request: Request):
        body = await self._body(request)
        name = _text(body.get("name"), "name")
        agents = body.get("allowed_agents", [])
        if not isinstance(agents, list) or not all(isinstance(item, str) and item for item in agents):
            raise HTTPException(status_code=422, detail="allowed_agents: a list of agent names is required")
        entry = {"type": MANAGER_TYPE, "enabled": True, "allowed_agents": agents}
        result = await self._run(self._store(request).create, name, entry, None, bool(body.get("dry_run")))
        if not body.get("dry_run"):
            logger.info("Agent editor: created the sub-agent manager %s in %s", name, result["file"])
        return result

    async def save_manager(self, request: Request, name: str):
        body = await self._body(request)
        entry, version = _mapping(body.get("entry"), "entry"), _text(body.get("version"), "version")
        result = await self._run(self._save_manager, self._store(request), name, entry, version, bool(body.get("dry_run")))
        if not body.get("dry_run") and result["diff"]:
            logger.info("Agent editor: saved the sub-agent manager %s", name)
        return result

    @staticmethod
    def _save_manager(store: Store, name: str, entry: dict, version: str, dry_run: bool) -> dict:
        """Only an enabled manager's own entry, and it stays one: its type and `enabled` are not changed here."""
        snap = store.snapshot()
        if name not in store.sams(snap):
            raise StoreError(404, f"{name} is not an enabled sub-agent manager")
        own = store.own(name, snap) or {}
        for key in ("type", "enabled"):
            if entry.get(key) != own.get(key):
                raise StoreError(400, f"{key}: a manager's {key} is not changed here")
        return store.save(name, entry, version, dry_run)

    # ------------------------------------------------------------------ pickers

    async def get_meta(self, request: Request):
        await self._admin(request)
        return await self._run(sources.meta, self._store(request), request.app.state)

    async def list_tools(self, request: Request):
        await self._admin(request)
        catalog = await sources.tool_catalog(request.app.state)
        snap = await self._run(self._store(request).snapshot)
        return {"servers": [{**row, "tools": [{**tool, "description": snap.mask(tool["description"])} for tool in row["tools"]]}
                            for row in catalog]}

    async def effective_tools(self, request: Request):
        body = await self._body(request)
        allowed, blocked = _string_list(body.get("allowed"), "allowed"), _string_list(body.get("blocked"), "blocked")
        name = _optional_text(body.get("name"), "name") or UNSAVED
        typ = _optional_text(body.get("type"), "type")
        catalog = await sources.tool_catalog(request.app.state)
        return await self._run(self._effective, self._store(request), name, typ, "type" not in body, allowed, blocked,
                               catalog)

    @staticmethod
    def _effective(store: Store, name: str, typ: Optional[str], type_from_disk: bool, allowed: Optional[list[str]],
                   blocked: Optional[list[str]], catalog: list[dict]) -> dict:
        """The lists `name` ends up with, resolved by the config's own merge: `type` and these lists as its entry.

        Without a `type` key the entry's type on disk applies; `"type": null` means no own type (basic_agent).
        """
        snap = store.snapshot()
        config = snap.config
        server = config.plugins.servers.get(name)
        if type_from_disk and server is not None and "type" in server.model_fields_set:
            typ = server.type
        # The panel sends lists as the files hold them (placeholders, masked answers copied over): read them as the
        # loader does.
        tools = {key: snap.expand(value) for key, value in (("allowed", allowed), ("blocked", blocked))
                 if value is not None}
        entry = {**({"type": typ} if typ else {}), **({"agent_config": {"tools": tools}} if tools else {})}
        try:
            resolved = resolve_with(config, name, entry)
        except Exception as error:
            raise StoreError(422, error_text(error))
        lists = resolved.agent_config.tools if resolved is not None and resolved.agent_config else None
        result = sources.effective_tools(catalog, list(lists.allowed or []) if lists else [],
                                         list(lists.blocked or []) if lists else [], sources.external_servers(config))
        # Matched unmasked, shown masked: the patterns are resolved values.
        return {**snap.mask({key: result[key] for key in ("allowed", "blocked", "unmatched", "per_tool")}),
                "external": {snap.mask(key): on for key, on in result["external"].items()},
                "tools": result["tools"], "counts": {snap.mask(key): n for key, n in result["counts"].items()}}

    async def read_prompt(self, request: Request, path: str, agent: Optional[str] = None):
        await self._admin(request)
        if unsafe_path(path):
            raise HTTPException(status_code=400, detail=f"{path}: only relative paths inside the repository")
        return await self._run(self._prompt, self._store(request), path, agent)

    @staticmethod
    def _prompt(store: Store, path: str, agent: Optional[str]) -> dict:
        files = store.files_of(agent) if agent and path.startswith(("./", "../")) else []
        target = ((files[-1].parent if files else store.root) / path).resolve()
        if not target.is_relative_to(store.root) or target.suffix.lower() not in PROMPT_SUFFIXES:
            raise StoreError(400, f"{path}: only {', '.join(PROMPT_SUFFIXES)} files inside the repository")
        if not target.is_file():
            return {"path": store.rel(target), "exists": False, "text": ""}
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(PROMPT_LIMIT + 1)
        return {"path": store.rel(target), "exists": True, "text": text[:PROMPT_LIMIT]}

    async def render_yaml(self, request: Request):
        body = await self._body(request)
        return {"yaml": dump(to_node(_mapping(body.get("entry"), "entry")))}

    async def parse_yaml(self, request: Request):
        text = (await self._body(request)).get("yaml")
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail="yaml: a string is required")
        try:
            anchored = has_anchors(text)
            entry = None if anchored else yaml.load(text, Loader=TypedLoader) or {}  # pure-yaml: libyaml refuses `!web/[ab]*`
        except Exception as error:  # a scanner error, but also e.g. ValueError for the date 2001-02-30
            raise HTTPException(status_code=422, detail=f"{type(error).__name__}: {error}")
        if anchored:
            raise HTTPException(status_code=422, detail="YAML anchors and aliases are not supported here")
        problem = unplain(entry)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        return {"entry": _mapping(entry, "yaml")}
