"""``lazy = true`` -- the manifest opt-in that allows building a server later.

Two halves that only work together. A plugin TYPE may declare that its
constructor is cheap and free of side effects, and the runtime holds it to
that promise: a lazy type that builds anything but an Agent is refused instead
of registered. Without the second half the flag would be a comment.

The price of "built later" is that a broken config stays quiet until the day
the agent is first used -- possibly weeks after somebody wrote it.
``Runtime.validate()`` pays it back: the checks that need no instance run at
start, for lazy declarations only.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_system.config.models import (
    AgentConfig, AgentSystemConfig, LLMModelConfig, LLMProfile, LLMSystemConfig,
    ToolServerConfig, PluginsConfig,
)
from agent_system.config.settings import load_settings
from agent_system.runtime import Runtime
from agent_system.servers.agent.server import Agent

REPO = Path(__file__).resolve().parents[2]


def _config(servers: dict[str, ToolServerConfig], *,
            plugin_dirs: list[str] | None = None,
            profiles: dict[str, LLMProfile] | None = None) -> AgentSystemConfig:
    return AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m", api_key="fake")},
            profiles=profiles or {"normal": LLMProfile(model_ref="m")},
            default_profile="normal",
        ),
        plugins=PluginsConfig(
            plugin_dirs=plugin_dirs or [str(REPO / "src" / "plugins")],
            servers=servers,
        ),
    )


def _agent_server(**kwargs) -> ToolServerConfig:
    return ToolServerConfig(type="basic_agent", enabled=True,
                     agent_config=AgentConfig(llm_profile="normal", **kwargs))


def _plugin_dir(root: Path, name: str, manifest_extra: str, body: str) -> None:
    plugin = root / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        f'[plugin]\nname = "{name}"\n{manifest_extra}', encoding="utf-8")
    (plugin / "plugin.py").write_text(body, encoding="utf-8")


AGENT_PLUGIN = (
    "from agent_system.plugins.factory_utils import make_agent_plugin_factory\n"
    "from agent_system.servers.agent.server import Agent\n"
    "PLUGIN_FACTORY = make_agent_plugin_factory(Agent)\n"
)


class TestTheFlagIsReadFromTheManifest:
    def test_the_shipped_basic_agent_declares_it(self):
        """117 of the 208 configured servers are of this type -- it is the one
        the lazy start is for. Read through the runtime, not out of the file:
        what counts is the value that reaches a declaration."""
        runtime = Runtime(_config({"probe_lazy": _agent_server()}))

        assert runtime.describe("probe_lazy").lazy is True

    def test_a_type_that_says_nothing_is_not_lazy(self, tmp_path):
        """Opt-in, not opt-out: `basic_agent` is an Agent factory and so are
        `writer_pipeline_v4` and `writer_story_designer`, whose constructors
        have not been read yet. Being an agent is not the promise."""
        root = tmp_path / "plugins"
        _plugin_dir(root, "silent_probe", "", AGENT_PLUGIN)

        # And a manifest that says something ELSE than the boolean: the
        # metadata is the raw TOML value, so a truthy string would enable a
        # deferred build on a promise nobody made. The JSON schema types the
        # key as a boolean; this is the runtime half of that.
        _plugin_dir(root, "stringly_probe", 'lazy = "yes"\n', AGENT_PLUGIN)

        runtime = Runtime(_config(
            {"probe_silent": ToolServerConfig(type="silent_probe", enabled=True,
                                       agent_config=AgentConfig(llm_profile="normal")),
             "probe_stringly": ToolServerConfig(type="stringly_probe", enabled=True,
                                         agent_config=AgentConfig(llm_profile="normal"))},
            plugin_dirs=[str(root)]))

        assert runtime.describe("probe_silent").lazy is False
        assert runtime.describe("probe_stringly").lazy is False, "a truthy string counted as the promise"


class TestALazyTypeMustBuildAnAgent:
    def test_every_type_that_declares_lazy_does(self):
        """The promise is about a constructor, and a constructor is only
        checked by running it.

        Reads the manifests of the CONFIGURED plugin directories, so a type
        marked lazy later is carried by this test without editing it.
        """
        from agent_system.plugins.catalog import PluginCatalog

        settings = load_settings()
        assert settings.plugins,             "fixture: load_settings() found no plugin config -- run pytest from the repo root"
        dirs = [str(d) for d in settings.plugins.plugin_dirs]
        catalog = PluginCatalog([Path(d) for d in dirs])
        lazy_types = sorted(t for t in catalog.types()
                            if (catalog.manifest(t) or {}).get("lazy") is True)

        assert lazy_types, "no type declares lazy = true -- this test measures nothing"
        for typ in lazy_types:
            name = f"probe_contract_{typ}"
            runtime = Runtime(_config({name: ToolServerConfig(
                type=typ, enabled=True,
                agent_config=AgentConfig(llm_profile="normal"))}, plugin_dirs=dirs))

            assert isinstance(runtime.materialize(name), Agent), \
                f"type '{typ}' declares lazy = true but did not build an Agent"

    def test_one_that_builds_a_plain_server_is_refused(self, tmp_path):
        """A manifest can put the flag on anything. Without this guard the
        non-agent would be skipped at start and then built inside whichever
        walker touched it first -- the start-up side effect moved to an
        arbitrary later moment, which is what the flag exists to avoid.

        The factory is a REAL shipped non-agent server, not a stand-in.
        """
        root = tmp_path / "plugins"
        _plugin_dir(root, "lazy_liar", "lazy = true\n",
                    "from plugins.file_ops.server import FileOpsServer\n"
                    "PLUGIN_FACTORY = FileOpsServer\n")

        runtime = Runtime(_config({"probe_liar": ToolServerConfig(type="lazy_liar", enabled=True)},
                                  plugin_dirs=[str(root)]))

        with pytest.raises(TypeError, match="not an Agent"):
            runtime.materialize("probe_liar")
        assert "probe_liar" not in runtime.registry.list(), "the refused server was registered anyway"


class TestValidateFindsWhatBuildingUsedToShow:
    def test_a_profile_pointing_at_a_model_that_is_gone(self):
        """The gap: ``_profiles_must_point_at_usable_models`` only looks at
        profiles whose ``model_ref`` EXISTS, and the settings chain check
        compares profile NAMES. A profile naming a deleted model passes both
        and dies at the agent's first request."""
        runtime = Runtime(_config({"probe_dangling": _agent_server()},
                                  profiles={"normal": LLMProfile(model_ref="ghost")}))

        findings = runtime.validate()

        assert len(findings) == 1, findings
        assert "probe_dangling" in findings[0] and "ghost" in findings[0]
        assert runtime.registry._servers == {}, "validation built an instance"

    def test_only_lazy_declarations_are_checked(self, tmp_path):
        """Both ways in one config, or this measures nothing.

        NOT because an eager server reports itself -- measured, it does not:
        ``Agent.__init__`` swallows the LLM error and the agent runs on with
        ``llm=None``. The line is drawn at ``lazy`` because that is the only
        set where "this declaration builds an Agent" is a checked promise.
        Every declaration inherits an ``agent_config``, ``file_ops`` included,
        so validating everything that has one would report the unused LLM
        profile of a file server.
        """
        root = tmp_path / "plugins"
        _plugin_dir(root, "eager_probe", "", AGENT_PLUGIN)

        runtime = Runtime(_config(
            {"probe_eager": ToolServerConfig(type="eager_probe", enabled=True,
                                      agent_config=AgentConfig(llm_profile="normal")),
             "probe_lazy": _agent_server()},
            plugin_dirs=[str(REPO / "src" / "plugins"), str(root)],
            profiles={"normal": LLMProfile(model_ref="ghost")}))

        findings = runtime.validate()

        assert [f for f in findings if "probe_lazy" in f], findings
        assert not [f for f in findings if "probe_eager" in f], \
            "an eager declaration was validated as well"

    def test_a_system_template_that_is_not_there(self, tmp_path):
        """Today this is a FileNotFoundError inside the first request's prompt
        render -- hours or days after the config was written."""
        template = tmp_path / "prompts" / "system.md"
        runtime = Runtime(_config({"probe_tpl": _agent_server(system_template=str(template))}))

        findings = runtime.validate()

        assert len(findings) == 1, findings
        assert "system_template" in findings[0] and str(template) in findings[0]

    def test_a_system_template_that_is_there_is_not_a_finding(self, tmp_path):
        """The other half of the check above: without it, 'reports a missing
        file' and 'reports every template' look the same."""
        template = tmp_path / "prompts" / "system.md"
        template.parent.mkdir(parents=True)
        template.write_text("You are a probe.", encoding="utf-8")
        runtime = Runtime(_config({"probe_tpl_ok": _agent_server(system_template=str(template))}))

        assert runtime.validate() == []

    def test_a_declaration_without_an_agent_config(self):
        """The one finding here that is fatal rather than degrading:
        ``Agent.__init__`` raises on a missing agent_config, so this server
        would not exist at all -- and lazily, nobody would notice until the
        request that needed it."""
        runtime = Runtime(_config({"probe_naked": ToolServerConfig(type="basic_agent", enabled=True)}))

        findings = runtime.validate()

        assert len(findings) == 1, findings
        assert "probe_naked" in findings[0] and "agent_config" in findings[0]

    def test_the_live_configuration_has_no_findings(self):
        """A guard measured against the real config, not a fixture: whatever
        else this suite proves, the 117 lazy servers of the shipped
        configuration must be clean -- otherwise every start logs errors and
        the operator learns to ignore them."""
        runtime = Runtime(load_settings())

        lazy = [name for name, decl in runtime.declarations().items() if decl.lazy]

        assert lazy, "no lazy declaration in the live config -- this test measures nothing"
        assert runtime.validate() == []

    def test_the_start_reports_them(self, caplog):
        """Where the check has to happen to be worth anything. Returning
        findings nobody calls for would be a function, not a guard."""
        runtime = Runtime(_config({"probe_started": _agent_server()},
                                  profiles={"normal": LLMProfile(model_ref="ghost")}))

        with caplog.at_level(logging.DEBUG, logger="agent_system.runtime"):
            runtime.start()

        # By RECORD, not by caplog.text: the declaration is logged at info and
        # debug with the server name in it, so a substring check over the whole
        # text passes even when validate() is never called at all (measured --
        # both mutations survived it).
        errors = [r.getMessage() for r in caplog.records
                  if r.levelno >= logging.ERROR and r.name == "agent_system.runtime"]
        assert [m for m in errors if "probe_started" in m], errors

    def test_it_never_takes_the_process_down(self, monkeypatch):
        """A broken LLM config is something this system deliberately survives:
        ``Agent.__init__`` swallows the error and leaves ``llm=None``, and the
        server keeps serving everything that is not the LLM. validate() reports
        that, it does not escalate it -- an earlier version raised under a test
        working directory, which would have killed ``start()`` for a config
        that builds. 10 of the shipped lazy agents would report a finding after
        a plain chdir (measured 2026-09-05: 21 carry a relative
        ``config/prompts/system_prompt.md``, 10 of them without a raw
        system_prompt that would win over it), so a working directory was all
        it took.
        """
        # Under a cwd that _in_test_cwd() accepts -- otherwise the earlier
        # raise could not have fired here either and this would measure
        # nothing. The repo root contains no "test".
        monkeypatch.setattr("agent_system.runtime.Path.cwd",
                            lambda: Path("/tmp/pytest-of-me/test_probe0"))
        runtime = Runtime(_config(
            {"probe_survives": _agent_server(system_template="does/not/exist.md")},
            profiles={"normal": LLMProfile(model_ref="ghost")}))

        findings = runtime.validate()
        runtime.start()

        assert len(findings) == 2, findings
        assert runtime.registry.get("probe_survives") is not None, "the server was not built"

    def test_a_template_behind_a_raw_system_prompt_is_not_reported(self):
        """RawPromptStrategy runs BEFORE TemplateFileStrategy, so an agent with
        a raw system_prompt never opens its template file. 11 of the shipped
        lazy servers carry both (measured); reporting their template would be a
        defect nobody can act on."""
        runtime = Runtime(_config({"probe_raw": _agent_server(
            system_prompt="You are a probe.", system_template="does/not/exist.md")}))

        assert runtime.validate() == []
