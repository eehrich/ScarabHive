"""The composition root: one place that turns a config into running servers.

Five entry points used to assemble the process each in their own way
(``build_app``, ``InitializationService``, ``ToolServerIntegration.initialize`` and
four writer tools), and each of them repeated the same twelve steps:
discover, merge the server config, call the factory with the right signature,
register in BOTH registries, then post-process an agent instance (shared
registry, metadata, description, visibility, self tool descriptions).

``Runtime`` owns those steps once. ``ServerDecl`` is what is known about a
server BEFORE it is built -- the merged config, its factory, its manifest --
so callers can ask about a server without instantiating it, which is what
makes lazy construction possible later.

Behaviour is deliberately identical to the old ``bootstrap_servers``:
same order, same log lines, same error policy (log and continue -- except
under a test working directory, where a failed instantiation re-raises).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .config.models import AgentSystemConfig, ToolServerConfig
from .config.settings import get_tool_server_config
from .tools.base import ToolServerRegistry

logger = logging.getLogger(__name__)

VISIBILITIES = ("ui", "tool", "both", "private")


def _in_test_cwd() -> bool:
    """Under a test working directory a swallowed failure is a green lie.

    Both halves are needed. The cwd substring alone is a production landmine:
    it matches ``/opt/agentsystem/releases/latest`` ("la-TEST-") and
    ``C:/Users/tester/...``, where a single broken plugin would then take the
    process down instead of being logged and skipped. And it is not enough on
    its own to mean "test" either -- the repo root, where the suite actually
    runs, contains no "test" at all, so this only ever fires for the tests
    that chdir into a pytest tmp directory. pytest sets PYTEST_CURRENT_TEST
    for every test it runs, which is what makes the pair safe.
    """
    return "test" in str(Path.cwd()) and "PYTEST_CURRENT_TEST" in os.environ


@dataclass(frozen=True)
class ServerView:
    """What a walker may ask about a server WITHOUT building it.

    Every per-request walk over the registry -- the UI agent list, the tool
    visibility filter that runs on every single request, the job manager --
    asks the same three questions and answers them the same way: build the
    server, isinstance it, read a flag. That is exactly what a lazy start
    cannot afford, and it is also more than those walkers need.

    The instance wins whenever there is one: its flags are what the process
    actually serves. The declaration answers only for a server that has not
    been built -- and only where it provably agrees, see ``Runtime.view``.

    ``getattr(..., True)`` on a built server is verbatim what the two
    converted walkers do (``tool_discovery._is_tool_visible`` and
    ``GET /agents``): a plugin carrying no visibility flag at all is VISIBLE.
    Agents always carry both flags (``Agent.__init__`` sets them False), so
    that default only ever reaches non-agent plugins.

    ⚠️ It is NOT the rule everywhere. ``sub_agent_manager/server.py`` reads
    the same two flags with ``getattr(..., False)`` and skips only when BOTH
    are false. A walker converted to this view without that in mind flips its
    own default -- which is why that one is not converted here.
    """

    name: str
    is_agent: bool
    tool_public: bool
    tool_visible: bool
    #: True when the answer came from an instance, False when from a
    #: declaration. Provenance, so a test can tell the two paths apart.
    built: bool


@dataclass
class ServerDecl:
    """What is known about a configured server before anything is built."""

    name: str
    type: str
    server_config: ToolServerConfig
    factory: Optional[Callable[..., Any]] = None      # None: the direct "agent" type
    plugin_metadata: Optional[dict] = field(default=None, repr=False)
    #: The plugin type that offered this server (``Runtime.declare``) -- None for one a config file names.
    offered_by: Optional[str] = None

    @property
    def is_declared_agent(self) -> bool:
        """True when this declaration builds an Agent for sure.

        Only the direct ``type: agent`` is certain without building; a plugin
        factory may return an agent or a plain server (writer_issues does both,
        by ``config.mode``).
        """
        return self.factory is None and self.type == "agent"

    @property
    def lazy(self) -> bool:
        """Manifest opt-in of the plugin TYPE: `lazy = true` in plugin.toml.

        It is a contract about the constructor -- config and an LLM client,
        no I/O, no thread, no socket -- and therefore a promise per type, not
        per class: `writer_pipeline_v4` and `writer_story_designer` build
        Agents as well, and their ``__init__`` has not been read yet. What
        the flag buys is that such a server may be built on first use instead
        of at start; ``materialize`` refuses to register a type that claims it
        and then builds something other than an Agent.
        """
        return (self.plugin_metadata or {}).get("lazy") is True

    @property
    def visibility(self) -> str:
        """ui | tool | both | private -- priority as bootstrap always had it:
        the instance's own ToolServerConfig metadata first, then the plugin manifest,
        else private (not visible; secure by default)."""
        metadata = self.server_config.metadata
        if metadata and metadata.visibility:
            return metadata.visibility
        if self.plugin_metadata:
            declared = self.plugin_metadata.get("visibility")
            if declared in VISIBILITIES:
                return declared
        return "private"

    def apply_to(self, instance: Any, registry: ToolServerRegistry) -> None:
        """The post-processing every agent instance got in bootstrap."""
        from .servers.agent.server import Agent

        if not isinstance(instance, Agent):
            return

        instance.registry = registry
        logger.debug("Updated agent %s to use shared registry with %d servers",
                     self.name, len(registry._servers))

        if self.server_config.metadata:
            instance._metadata = self.server_config.metadata.model_dump()
            logger.debug("Applied instance metadata to agent '%s': %s", self.name, instance._metadata)

        if self.server_config.description:
            instance._description = self.server_config.description
            logger.debug("Applied instance description to agent '%s': %s",
                         self.name, self.server_config.description)

        if not hasattr(instance, "_visibility_set_explicitly"):
            visibility = self.visibility
            instance._tool_public = visibility in ("ui", "both")
            instance._tool_visible = visibility in ("tool", "both")
            logger.debug("Plugin agent '%s' visibility set: %s (ui=%s, tool=%s)",
                         self.name, visibility, instance._tool_public, instance._tool_visible)

        if self.server_config.self_tool_descriptions:
            instance._self_tool_descriptions = self.server_config.self_tool_descriptions
            logger.debug("Applied self_tool_descriptions to agent '%s': %d overrides",
                         self.name, len(instance._self_tool_descriptions))


class Runtime:
    """Config in, running servers out -- and the declarations in between."""

    #: The runtime whose start() ran last in this process: the servers the
    #: process actually runs (read by the system status).
    last_started: Optional["Runtime"] = None

    def __init__(self, config: AgentSystemConfig, *,
                 registry: Optional[ToolServerRegistry] = None,
                 session_service: Any = None):
        self.config = config
        self.registry = registry if registry is not None else ToolServerRegistry()
        self._session_service = session_service
        self._decls: dict[str, ServerDecl] = {}
        self._plugins: dict[str, Callable[..., Any]] = {}
        #: Servers that did not start (unresolved config, unknown type, a
        #: failed build), one line each -- the start-up policy logs and carries
        #: on, so this is the only record a status page can show.
        self.problems: list[str] = []
        #: validate()'s findings from the last start(): config errors of lazy
        #: agents, which may still run (degraded) or fail again as a problem.
        self.config_findings: list[str] = []
        self._declare_all()
        # Bound registries answer describe()/built() from here; an
        # unbound ToolServerRegistry keeps behaving exactly as it always did.
        self.registry.bind(self)

    # ------------------------------------------------------------------
    # declarations
    # ------------------------------------------------------------------
    def _declare_all(self) -> None:
        if not self.config.plugins:
            logger.warning("No plugins configuration found, skipping bootstrap")
            return

        configured = self.config.plugins.plugin_dirs or []
        dirs = [Path(p) for p in configured if p]
        from .plugins import discover_all_plugins
        self._plugins = discover_all_plugins(dirs=dirs if dirs else None)

        if self._plugins:
            logger.info("Discovered plugins: %s", ", ".join(sorted(self._plugins.keys())))
        else:
            logger.debug("No external plugins discovered")

        for name, server in self.config.plugins.servers.items():
            if not server.enabled:
                continue
            merged = get_tool_server_config(name, self.config)
            if not merged:
                logger.warning("Failed to resolve tool server config for server '%s', skipping", name)
                self.problems.append(f"server '{name}': config could not be resolved")
                continue
            logger.debug(f"Bootstrap server '{name}': type={merged.type}, enabled={merged.enabled}")

            factory = self._plugins.get(merged.type)
            if factory is not None:
                metadata = getattr(factory, "_plugin_metadata", None)
                if metadata:
                    logger.info("Using plugin '%s' (version=%s) for server '%s': %s",
                                merged.type, metadata.get("version") or "",
                                name, metadata.get("description") or metadata.get("summary") or "")
            elif merged.type != "agent":
                logger.warning("Unknown server type '%s' for server '%s'", merged.type, name)
                self.problems.append(f"server '{name}': unknown type '{merged.type}'")
                continue

            self._decls[name] = ServerDecl(
                name=name, type=merged.type, server_config=merged, factory=factory,
                plugin_metadata=getattr(factory, "_plugin_metadata", None),
            )
        self._declare_offered()

    def _declare_offered(self) -> None:
        """The servers plugin types offer beyond the config: a factory with ``offered_servers(config) -> {name:
        entry}`` (stategraph_machine: the machines whose file has an ``agent:`` block). Asked in every process that
        builds a runtime, before anything is built -- so start() builds them like the configured ones."""
        for type_name, factory in sorted(self._plugins.items()):
            offer = getattr(factory, "offered_servers", None)
            if offer is None:
                continue
            try:
                offered = dict(offer(self.config) or {})
            except Exception as e:  # a broken offer costs its servers, not the start
                logger.warning("Plugin '%s' could not offer its servers: %s", type_name, e, exc_info=True)
                self.problems.append(f"plugin '{type_name}': offered_servers failed: {e}")
                continue
            for name, entry in offered.items():
                try:
                    self.declare(name, entry, offered_by=type_name)
                except ValueError as e:
                    logger.warning("Plugin '%s' offers server '%s': %s", type_name, name, e)
                    self.problems.append(f"server '{name}' offered by '{type_name}': {e}")

    def declare(self, name: str, entry: dict, *, offered_by: str) -> ServerDecl:
        """Declare a server no config file names, from its config ``entry`` (the mapping a ``plugins.servers``
        entry would hold). It enters ``config.plugins.servers`` too, so every lookup by name finds it as it finds a
        configured one; start() builds it -- after start, ``materialize(name)`` does. A name the config (or an
        earlier offer) holds is refused: ValueError."""
        servers = self.config.plugins.servers
        if name in servers or name in self._decls:
            raise ValueError("the name is taken by a configured server")
        try:
            servers[name] = ToolServerConfig.model_validate(entry)
            merged = get_tool_server_config(name, self.config)
            factory = self._plugins.get(merged.type) if merged is not None else None
            if factory is None:
                raise ValueError(f"unknown type '{getattr(merged, 'type', None)}'")
        except Exception as e:
            servers.pop(name, None)
            raise ValueError(f"not declared: {e}") from e
        decl = self._decls[name] = ServerDecl(
            name=name, type=merged.type, server_config=merged, factory=factory,
            plugin_metadata=getattr(factory, "_plugin_metadata", None), offered_by=offered_by)
        logger.info("Declared server '%s' (type %s), offered by plugin '%s'", name, merged.type, offered_by)
        return decl

    def carry_offered(self, config: AgentSystemConfig) -> None:
        """A config read afresh (a reload) knows only its files: the servers plugins offered at the start go into it
        as they were declared -- they run as that, until the next start offers anew."""
        servers = config.plugins.servers if config.plugins else None
        if servers is None:
            return
        for name, decl in self._decls.items():
            if decl.offered_by is not None and name not in servers and name in self.config.plugins.servers:
                servers[name] = self.config.plugins.servers[name]

    def declarations(self) -> dict[str, ServerDecl]:
        return dict(self._decls)

    def describe(self, name: str) -> Optional[ServerDecl]:
        """What is known about a server without building it."""
        return self._decls.get(name)

    def view(self, name: str) -> Optional[ServerView]:
        """The walker-facing answer: from the instance if built, else from the
        declaration -- and None whenever the declaration cannot answer.

        None means "ask the instance", and every caller falls back to doing
        exactly that. It is the only honest answer for a declaration whose
        instance would disagree, and disagreement is the norm, not the corner
        case: ``apply_to`` copies the visibility onto the instance ONLY for a
        plugin factory (``materialize``: ``if decl.factory is not None``) and
        returns early for anything that is not an ``Agent``. So

          * a non-agent plugin (file_ops, terminal, todo, ...) never carries
            the flags at all -- built it reads (True, True) through the
            getattr defaults, while its declaration defaults to "private",
            i.e. (False, False). Answering from the declaration would drop
            every tool server out of every agent's tool list.
          * a direct ``type: agent`` gets no ``apply_to`` either and keeps
            ``Agent.__init__``'s (False, False), whatever its metadata says.
          * a non-lazy plugin type may or may not build an Agent
            (``writer_pipeline_v4`` does, ``writer_issues`` decides by
            config) -- ``is_agent`` is not knowable without the constructor.

        ``lazy`` is the one declaration that is provably safe: it is a plugin
        type (so ``apply_to`` runs and the flags ARE the visibility mapping),
        and ``materialize`` refuses to register a lazy type that builds
        something other than an Agent. That is also exactly the set B5 defers
        -- everything else is built at start, so its fallback finds an
        instance.
        """
        instance = self.registry._servers.get(name)
        if instance is not None:
            from .servers.agent.server import Agent
            return ServerView(
                name=name,
                is_agent=isinstance(instance, Agent),
                tool_public=bool(getattr(instance, "_tool_public", True)),
                tool_visible=bool(getattr(instance, "_tool_visible", True)),
                built=True,
            )

        decl = self._decls.get(name)
        if decl is None or not decl.lazy:
            return None
        visibility = decl.visibility
        return ServerView(
            name=name,
            is_agent=True,
            tool_public=visibility in ("ui", "both"),
            tool_visible=visibility in ("tool", "both"),
            built=False,
        )

    def validate(self) -> list[str]:
        """Config errors of LAZY declarations, found without an instance.

        Lazy is the set where "this declaration builds an Agent" is a promise
        the manifest made and ``materialize`` enforces -- so it is the set
        whose agent_config can be judged without building anything.

        NOT the whole story, and the gap is named on purpose: an EAGER agent
        type with the same broken config also degrades quietly, because
        ``Agent.__init__`` swallows the LLM error and leaves ``llm=None``
        (measured 2026-09-05: one model removed from llm.yaml, 50 agents built
        fine and warned 50 times). Widening this loop is not the fix -- every
        declaration carries an inherited ``agent_config``, ``file_ops``
        included, so validating "everything that has one" would report the
        unused LLM profile of a file server. The fix for that half sits where
        the instance exists: the warning in ``Agent.__init__`` names its agent.

        Few checks, because most of a declaration is already judged earlier
        and a second guard for the same thing only drifts apart from the
        first. Measured 2026-09-05 against the pydantic models, after two
        claims in an earlier version of this docstring turned out to be
        wrong: config LOAD already rejects an unknown ``provider:``, a
        ``provider: batch`` without a backend, and any ``llm_params`` that do
        not validate (``AgentConfig._validate_llm_params`` runs each of them
        through ``LLMModelConfig``, flat form and profile-keyed). A profile
        that exists nowhere is logged per agent by ``settings``, over the
        whole chain -- more than this sees.

        Three things are left, and nothing else covers them:

        - a profile whose ``model_ref`` was deleted from llm.yaml --
          ``_profiles_must_point_at_usable_models`` looks only at model_refs
          that EXIST, and the chain check compares profile NAMES;
        - a missing ``agent_config``, which ``Agent.__init__`` raises on;
        - a ``system_template`` whose file is not there, today a
          FileNotFoundError in the first request's prompt render.

        Only the PRIMARY profile is resolved, exactly as ``Agent.__init__``
        does; the fallbacks of the chain are the ``settings`` check's job.

        Returns the findings; each is logged once as an ``error``. It does NOT
        raise: a broken LLM config is something the system deliberately
        survives (``Agent.__init__`` leaves ``llm=None``), and turning that
        into a dead process would be a policy change nobody asked for.
        """
        findings: list[str] = []
        for name, decl in self._decls.items():
            if not decl.lazy:
                continue
            findings.extend(self._findings_for(name, decl))
        return findings

    def _findings_for(self, name: str, decl: ServerDecl) -> list[str]:
        agent_config = decl.server_config.agent_config
        if agent_config is None:
            # Agent.__init__ raises on this one -- so this is the only finding
            # here that would take the server down rather than degrade it.
            return [self._report(name, "has no agent_config")]

        from .llm.factory import resolve_llm_config_for_agent

        findings = []
        try:
            resolve_llm_config_for_agent(self.config, agent_config)
        except Exception as e:
            findings.append(self._report(name, f"LLM config does not resolve: {e}"))

        # Only when the file would actually be opened. RawPromptStrategy sits
        # BEFORE TemplateFileStrategy in PromptRenderer, so an agent carrying a
        # raw system_prompt never reads its template -- 11 of the shipped lazy
        # servers are in exactly that state (measured 2026-09-05), and
        # reporting their template would be a defect nobody can act on.
        template = agent_config.system_template
        if template and not agent_config.system_prompt and not Path(template).is_file():
            findings.append(self._report(name, f"system_template '{template}' does not exist"))
        return findings

    @staticmethod
    def _report(name: str, problem: str) -> str:
        finding = f"agent '{name}': {problem}"
        logger.error("Lazy agent validation -- %s", finding)
        return finding

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def start(self) -> "Runtime":
        """Build every declared server, in config order."""
        Runtime.last_started = self
        self.config_findings = self.validate()
        for name in list(self._decls):
            self._build_logged(name)
        try:
            logger.info("Registered tool servers: %s", ", ".join(self.registry.list()))
        except Exception:
            logger.debug("Could not list registered servers after bootstrap")
        return self

    def _build_logged(self, name: str) -> Any:
        """Today's start-up error policy: log and carry on, so one broken
        server does not take the process with it -- but re-raise under a test
        working directory, where a swallowed failure is a green lie."""
        decl = self._decls[name]
        try:
            return self.materialize(name)
        except Exception as e:
            if decl.factory is not None:
                logger.exception("Failed to instantiate plugin '%s' for server '%s': %s",
                                 decl.type, name, e)
            else:
                logger.exception("Failed to instantiate agent '%s': %s", name, e)
            self.problems.append(f"server '{name}': failed to start: {e}")
            if _in_test_cwd():
                raise
            return None

    def materialize(self, name: str) -> Any:
        """THE one build path: construct, register in both registries, post-process."""
        # Idempotent: a name already in the registry is handed back, NOT built
        # a second time -- that is what makes a later lazy build safe to call
        # from anywhere. It also means an instance somebody else registered
        # under this name keeps its place (bootstrap used to overwrite it) and
        # then never sees the plugin-registry entry or the post-processing.
        # No caller can reach that today: every one of them passes a fresh
        # ToolServerRegistry, and the only other writer, create_and_register_agent,
        # runs after the bootstrap.
        existing = self.registry._servers.get(name)
        if existing is not None:
            return existing

        decl = self._decls.get(name)
        if decl is None:
            raise KeyError(name)

        instance = self._construct(decl)
        if decl.lazy:
            # The manifest promised an Agent (see ServerDecl.lazy). Refuse
            # here rather than register it: once the start skips lazy servers,
            # a lazy non-agent would materialize inside whichever walker
            # touched it first -- a start-up side effect moved to an arbitrary
            # later moment, which is what `lazy` exists to avoid.
            #
            # The cost of refusing is that the server is GONE for this process
            # (``_build_logged`` logs the exception and returns None) although
            # it would have worked. That is the intended trade: a wrong `lazy`
            # is a manifest bug, and it has to be loud on the first start
            # rather than surprising somebody in week three. The constructor
            # has already run at this point, so whatever it grabbed stays
            # grabbed -- another reason the flag belongs on types whose
            # __init__ was read.
            from .servers.agent.server import Agent
            if not isinstance(instance, Agent):
                raise TypeError(
                    f"server '{name}': type '{decl.type}' declares lazy = true in its "
                    f"manifest but built a {type(instance).__name__}, not an Agent")
        self.registry.register(name, instance)

        if decl.factory is not None:
            # Asymmetric on purpose, exactly as bootstrap always was: the
            # direct ``type: agent`` branch registered the instance in the
            # ToolServerRegistry and did nothing else. Doing the two steps below for
            # it as well would publish it as an plugin server and give it
            # a visibility flag it never had -- and the default, "private",
            # would take it OUT of GET /agents, which shows agents that carry
            # no _tool_public at all.
            from .plugins.tool_adapter import plugin_tool_registry
            plugin_tool_registry.register_existing_plugin_instance(
                name, instance, self.config, decl.server_config)
            logger.debug(f"Registered plugin '{name}' in both registries (ToolServerRegistry + PluginToolRegistry)")

            decl.apply_to(instance, self.registry)
        if self._session_service is not None:
            from .servers.agent.server import Agent
            if isinstance(instance, Agent):
                instance._session_service = self._session_service
        return instance

    def _construct(self, decl: ServerDecl) -> Any:
        if decl.factory is not None:
            # All plugins take (name, system_config, server_config); agent
            # factories additionally take the shared registry, and it must
            # reach __init__ -- see plugins/factory_utils.
            if getattr(decl.factory, "_accepts_registry", False):
                return decl.factory(decl.name, self.config, decl.server_config, registry=self.registry)
            return decl.factory(decl.name, self.config, decl.server_config)

        from .servers.agent.server import Agent
        return Agent(decl.name, self.config, decl.server_config, self.registry)


def configure_process_singletons(config: AgentSystemConfig) -> None:
    """Process-wide setup that must happen ONCE, before any server is built.

    (Historically the cancellation manager was configured in every
    Agent.__init__, which REPLACED the global manager per instantiation and
    orphaned all registered tokens/tasks -- cancellation of in-flight requests
    silently stopped working. The old code also read a non-existent top-level
    `config.cancellation`, so the YAML values were never honored;
    CancellationConfig lives under `logging.cancellation`.)
    """
    from .core.cancellation import configure_cancellation_manager
    cancellation = getattr(getattr(config, "logging", None), "cancellation", None)
    if cancellation:
        configure_cancellation_manager(
            cleanup_timeout=cancellation.cleanup_timeout,
            monitor_interval=cancellation.monitor_interval,
        )
