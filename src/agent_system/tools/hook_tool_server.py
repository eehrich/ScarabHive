"""The base class for a plugin that serves tools AND hooks at once.

Six plugins do both today (todo, memory, lessons_learned, context_engineer,
context_summarizer, sub_agent_manager), and until now each one wired the two
halves together itself. The two base classes cannot be combined by a plain
``super().__init__()``: their signatures disagree -- a tool server is built
from ``(name, system_config, server_config)``, a hook from ``(name, config)``
-- so ``ToolServer.__init__`` deliberately does not call up the chain, and
whoever inherits both had to call both by hand.

Four spellings of that had grown, and measured against a live object they did
three different things:

    todo                  hook config = schema.yaml defaults (own copy of the
                          extractor), so a ``hook_config:`` block in
                          plugins.yaml was silently ignored
    context_engineer      hook config = plugins.yaml ``hook_config`` only, so
    context_summarizer    the defaults written in schema.yaml never applied
    sub_agent_manager
    memory                PluginHook.__init__ never ran: no ``self.config``,
    lessons_learned       no ``self.enabled``, and ``get_order_spec()`` --
                          which ``HookRegistry.register_hook`` calls whenever a
                          caller passes no explicit order -- raised
                          AttributeError. Latent, not live: the one production
                          registration path (``discovery.py``) always passes an
                          order, and nobody reads ``hook.enabled``. So what
                          this class fixes is the config rule; the missing
                          attributes were a trap, not a crash.

This class is the single answer: the hook config is the schema defaults with
the plugins.yaml block on top, the same rule ``SchemaBasedPluginHook`` uses for
hook-only plugins.

It is built LAZILY, on the first read of ``config``, and that is not a
performance detail: rendering a schema may need the subclass's own state
(``sub_agent_manager``'s template asks for ``phase_filtering_enabled``), which
does not exist yet while the base initialiser runs. A constructor that reads
the schema would therefore work for five plugins and raise for the sixth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_system.core.schema_base_mixin import config_defaults_from_schema
from agent_system.hooks.plugin_hook import PluginHook

from .schema_based import SchemaBasedToolServer

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class SchemaBasedHookToolServer(SchemaBasedToolServer, PluginHook):
    """A tool server that is also a hook, with both halves initialised.

    Subclasses keep their own ``__init__`` for whatever else they set up and
    call ``super().__init__(name, system_config, server_config)`` once -- not
    the two base initialisers by hand.

    ``config`` and ``enabled`` can still be assigned; a subclass that wants a
    different mapping (context_engineer hands its whole ``config:`` block to
    its compaction strategy) just sets them and the lazy build steps aside.
    """

    # Class-level so that even a subclass which never reaches this __init__
    # (the hand-rolled `SchemaBasedToolServer.__init__(self, ...)` that all six
    # plugins used before this class existed) reads a config instead of an
    # AttributeError out of the property below.
    _hook_config: dict[str, Any] | None = None
    _hook_enabled: bool | None = None
    _hook_config_warned: bool = False

    def __init__(self, name: str, system_config: AgentSystemConfig,
                 server_config: ToolServerConfig):
        # Before the tool half: its initialiser may already touch `config`,
        # and the property below must find the cache attribute in place.
        self._hook_config = None
        self._hook_enabled = None
        self._hook_config_warned = False
        SchemaBasedToolServer.__init__(self, name, system_config, server_config)

    # -- PluginHook's two attributes, as lazily built properties -------------

    @property
    def config(self) -> dict[str, Any]:
        """The hook's configuration: schema defaults, plugins.yaml on top."""
        if self._hook_config is None:
            try:
                self._hook_config = self._build_hook_config()
            except Exception as exc:
                # A schema.yaml that cannot be read, a `config:` that is not a
                # mapping, a `hook_config:` that is not one either: all of that
                # is a bad config, not a reason to fail every LLM call this hook
                # runs in. Nothing is cached here, so a repaired file takes
                # effect without a restart -- the price is that a broken one is
                # read again on every call, which is why the warning is said
                # once and not per step.
                if not self._hook_config_warned:
                    self._hook_config_warned = True
                    logger.warning("Hook config for %s unusable (%s) -- running on "
                                   "whatever of it can be read", self.name, exc)
                return self._operator_config()
        return self._hook_config

    @config.setter
    def config(self, value: dict[str, Any] | None) -> None:
        self._hook_config = dict(value or {})

    @property
    def enabled(self) -> bool:
        """Whether this hook runs at all. From the config unless set."""
        if self._hook_enabled is not None:
            return self._hook_enabled
        return bool(self.config.get("enabled", True))

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._hook_enabled = bool(value)

    # -- how the config is built --------------------------------------------

    def _build_hook_config(self) -> dict[str, Any]:
        """Schema defaults, with the plugins.yaml ``hook_config`` block on top.

        Both sources, in that order: the schema is where a plugin writes what
        its hook does by default, and plugins.yaml is where the operator says
        otherwise. Taking only one of them is what the six plugins used to do,
        each a different one.

        Raises whatever the schema throws -- the caller decides what a broken
        schema costs, and it must not cost a cached wrong answer.
        """
        config = config_defaults_from_schema(
            (self.get_schema_data() or {}).get("config", {}))
        config.update(getattr(self.server_config, "hook_config", None) or {})
        return config

    def _operator_config(self) -> dict[str, Any]:
        """Only what plugins.yaml says, and only if it is readable at all.

        The fallback for a build that failed, so it may not fail for the same
        reason: a ``hook_config:`` written as a list or a string is exactly one
        of the things that can break the build, and re-reading it unchecked
        would raise out of the property instead of the hook running without
        its options.
        """
        raw = getattr(self.server_config, "hook_config", None)
        return dict(raw) if isinstance(raw, dict) else {}
