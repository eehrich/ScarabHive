"""The base class for a plugin that is a tool server AND a hook.

Six plugins are both, and before this class existed each one joined the two
halves by hand -- in four spellings that did three different things. What is
pinned here is the one rule they now share, and the two traps that produced
the old defects.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_system.core.schema_base_mixin import config_defaults_from_schema
from agent_system.tools.hook_tool_server import SchemaBasedHookToolServer
from agent_system.tools.schema_based import SchemaBasedToolServer

SCHEMA = {
    "config": {
        "max_items": {"type": "integer", "default": 20},
        "format": {"type": "string", "default": "markdown"},
        "plain_value": 7,
    }
}


class Plugin(SchemaBasedHookToolServer):
    """A plugin whose schema is handed in rather than read from disk."""

    def __init__(self, server_config, schema=SCHEMA, needs=None):
        self._schema_to_return = schema
        self._needs = needs
        super().__init__("test_plugin", SimpleNamespace(), server_config)

    def get_schema_data(self):
        # sub_agent_manager's real case: rendering the schema reads an
        # attribute the subclass sets AFTER super().__init__() returns.
        if self._needs is not None and not hasattr(self, self._needs):
            raise RuntimeError(f"no attribute {self._needs}")
        return self._schema_to_return


def a_config(**kwargs):
    return SimpleNamespace(**kwargs)


class TestTheHookConfig:

    def test_the_schema_defaults_are_the_configuration(self):
        plugin = Plugin(a_config())

        assert plugin.config == {"max_items": 20, "format": "markdown", "plain_value": 7}

    def test_plugins_yaml_wins_over_the_schema(self):
        """The schema says what the plugin does by default, the operator says otherwise."""
        plugin = Plugin(a_config(hook_config={"max_items": 5, "extra": True}))

        assert plugin.config["max_items"] == 5, "the operator's value must win"
        assert plugin.config["format"] == "markdown", "an untouched default stays"
        assert plugin.config["extra"] is True, "a key only plugins.yaml knows arrives"

    def test_a_subclass_may_hand_in_its_own_mapping(self):
        """context_engineer passes its whole `config:` block on to its strategy."""
        plugin = Plugin(a_config(hook_config={"max_items": 5}))
        plugin.config = {"something": "else"}

        assert plugin.config == {"something": "else"}

    def test_enabled_comes_from_the_config_and_can_be_set(self):
        assert Plugin(a_config()).enabled is True
        assert Plugin(a_config(hook_config={"enabled": False})).enabled is False

        plugin = Plugin(a_config(hook_config={"enabled": False}))
        plugin.enabled = True
        assert plugin.enabled is True, "an explicit value wins over the config"


class TestTheTwoTraps:

    def test_the_config_is_built_lazily_not_in_the_constructor(self):
        """Reading the schema while __init__ runs is too early for some plugins.

        sub_agent_manager's schema template asks for `phase_filtering_enabled`,
        which its own __init__ sets after the base returns. A constructor that
        renders the schema works for five plugins and raises for the sixth.
        """
        plugin = Plugin(a_config(), needs="phase_filtering_enabled")
        plugin.phase_filtering_enabled = False  # as the real subclass does

        assert plugin.config["max_items"] == 20

    def test_the_registry_can_ask_for_the_order_without_an_explicit_init(self):
        """memory and lessons_learned never ran PluginHook.__init__.

        `HookRegistry.register_hook` calls `get_order_spec()` whenever a caller
        passes no order of its own, and that reads `self.config` -- which did
        not exist on those two.
        """
        plugin = Plugin(a_config())

        assert plugin.get_order_spec() == {"before": [], "after": []}

    def test_an_unreadable_schema_leaves_the_plugin_usable(self):
        """A broken schema is a bad config, not a reason to fail the server."""
        plugin = Plugin(a_config(hook_config={"max_items": 3}), needs="never_set")

        assert plugin.config == {"max_items": 3}

    def test_a_config_block_that_is_not_a_mapping_stays_inside(self):
        """A `config:` written as a list is a typo, not an exception per call.

        The config is read in the middle of a hook run, so anything escaping
        here turns into a failed LLM call -- every single one, silently.
        """
        plugin = Plugin(a_config(hook_config={"max_items": 3}),
                        schema={"config": ["max_items: 20"]})

        assert plugin.config == {"max_items": 3}

    def test_a_hook_config_that_is_not_a_mapping_does_not_raise_either(self):
        """The fallback may not fail for the same reason the build did.

        `hook_config:` written as a list is one of the things that breaks the
        build -- and the fallback reads exactly that key again. Reading it
        unchecked turns the rescue into the same exception, in the middle of
        every hook run.
        """
        plugin = Plugin(a_config(hook_config=["enabled"]))

        assert plugin.config == {}
        assert plugin.enabled is True, "a hook must still run without its options"

    def test_the_degraded_config_is_not_cached(self):
        """A fixed schema takes effect without a restart -- so nothing is kept."""
        plugin = Plugin(a_config(hook_config={"max_items": 3}), needs="repaired")
        assert plugin.config == {"max_items": 3}, "fixture: the schema must fail first"

        plugin.repaired = True  # e.g. the file was fixed and the cache cleared

        assert plugin.config["format"] == "markdown", (
            "the schema defaults never arrived: the degraded config was cached")


    def test_a_subclass_that_never_reaches_this_init_still_reads_a_config(self):
        """The old hand-rolled spelling must degrade, not raise.

        Calling ``SchemaBasedToolServer.__init__`` directly is what all six
        plugins did before this class existed, and what the old docs showed.
        Getting an AttributeError out of a property for it would be worse than
        the missing attribute it replaced.
        """
        class WrittenTheOldWay(Plugin):
            def __init__(self, server_config):
                self._schema_to_return, self._needs = SCHEMA, None
                SchemaBasedToolServer.__init__(
                    self, "old_style", SimpleNamespace(), server_config)

        plugin = WrittenTheOldWay(a_config(hook_config={"max_items": 5}))

        assert plugin.config["max_items"] == 5
        assert plugin.enabled is True


class TestTheDefaultsExtractor:

    @pytest.mark.parametrize("given,expected", [
        ({"a": {"type": "integer", "default": 1}}, {"a": 1}),
        ({"a": 1}, {"a": 1}),                                   # short form
        ({"a": {"type": "integer"}}, {"a": {"type": "integer"}}),  # no default: as written
        ({}, {}),
        (None, {}),
    ])
    def test_it_reads_both_spellings(self, given, expected):
        assert config_defaults_from_schema(given) == expected
