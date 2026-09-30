"""otel: shipped off, content off by default, config reaching the plugin, and
the exporters it builds -- asserted on the resolved configuration and on what
the plugin actually builds, not on file text."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from agent_system.config.models import ToolServerConfig
from agent_system.config.settings import get_tool_server_config, load_settings
from agent_system.hooks import HookContext, HookType
from agent_system.plugins.discovery import discover_plugins
from plugins.otel import hooks as otel_hooks
from plugins.otel import telemetry
from plugins.otel.plugin import PLUGIN_FACTORY
from plugins.otel.settings import TelemetrySettings

PLUGIN_DIR = Path(otel_hooks.__file__).resolve().parent


@pytest.fixture(scope="module")
def config():
    return load_settings()


def _plugin(**config):
    return PLUGIN_FACTORY("otel", None, ToolServerConfig(type="otel", enabled=True, config=config))


class TestShippedConfig:

    def test_the_plugin_ships_disabled(self, config):
        raw = config.plugins.servers.get("otel")
        assert raw is not None, "config/plugins.yaml has no otel entry"
        assert raw.type == "otel"
        # `enabled` is read from the raw entry (the activation chain).
        assert raw.enabled is False

    def test_the_shipped_config_block_has_no_dead_key(self, config):
        server = get_tool_server_config("otel", config)
        block = dict(server.config or {})
        assert block, "fixture: the otel entry carries no config block"
        unknown = set(block) - set(TelemetrySettings.__dataclass_fields__)
        assert not unknown, f"keys the plugin never reads: {unknown}"
        plugin = PLUGIN_FACTORY("otel", config, server)
        for key, value in block.items():
            assert getattr(plugin.settings, key) == value, key

    def test_every_setting_reaches_the_plugin_from_its_server_config(self):
        """Values unlike every default: a key that got lost would show its default."""
        wanted = dict(exporter="console", protocol="http/protobuf", endpoint="http://c.test:4318",
                      headers={"authorization": "Bearer t"}, insecure=True, service_name="svc",
                      capture_content=True, content_max_chars=321, capture_user_id=True,
                      max_open_runs=7, max_pending_tool_calls=9, idle_timeout_seconds=11.0,
                      metrics=True, metrics_export_interval_seconds=13.0,
                      shutdown_timeout_seconds=1.5)
        assert set(wanted) == set(TelemetrySettings.__dataclass_fields__), "fixture: a setting is missing"
        defaults = TelemetrySettings()
        assert all(getattr(defaults, key) != value for key, value in wanted.items()), \
            "fixture: a value equals its default"

        settings = _plugin(**wanted).settings

        assert {key: getattr(settings, key) for key in wanted} == wanted

    def test_discovery_finds_the_plugin_type(self):
        assert "otel" in discover_plugins(PLUGIN_DIR.parent)


class TestDefaults:

    def test_content_and_user_ids_stay_in_by_default(self):
        plugin = _plugin()
        assert plugin.settings.capture_content is False
        assert plugin.settings.capture_user_id is False
        assert TelemetrySettings.from_config({}).capture_content is False

    def test_every_schema_key_has_a_default_the_settings_read(self):
        plugin = _plugin()
        schema_keys = set(plugin.get_schema_data()["config"])
        assert schema_keys == set(TelemetrySettings.__dataclass_fields__)
        assert TelemetrySettings.from_config(plugin.get_config()) == TelemetrySettings()

    @pytest.mark.parametrize("value", ["maybe", 3, [True]])
    def test_a_capture_switch_that_is_not_a_boolean_stays_off(self, value):
        assert TelemetrySettings.from_config({"capture_content": value}).capture_content is False

    def test_an_unknown_exporter_falls_back_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            settings = TelemetrySettings.from_config({"exporter": "jaeger"})
        assert settings.exporter == "otlp"
        assert "jaeger" in caplog.text

    @pytest.mark.parametrize("key, value", [
        ("capture_content", "maybe"), ("insecure", "sometimes"), ("max_open_runs", True),
        ("content_max_chars", "inf"), ("idle_timeout_seconds", "six hours"),
        ("shutdown_timeout_seconds", 0)])
    def test_a_value_that_does_not_fit_falls_back_with_a_warning(self, key, value, caplog):
        default = False if key == "insecure" else getattr(TelemetrySettings(), key)
        with caplog.at_level(logging.WARNING):
            settings = TelemetrySettings.from_config({key: value})
        assert getattr(settings, key) == default
        assert key in caplog.text

    def test_an_unset_value_takes_its_default_silently(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert TelemetrySettings.from_config({"max_open_runs": None, "content_max_chars": "", "metrics": ""}) \
                == TelemetrySettings()
        assert not caplog.text

    def test_a_header_set_twice_ignoring_case_is_named(self, caplog):
        with caplog.at_level(logging.WARNING):
            settings = TelemetrySettings.from_config(
                {"headers": {"Authorization": "a", "authorization": "b", "x-other": "c"}})
        assert settings.headers == {"authorization": "b", "x-other": "c"}
        assert "'authorization' is set twice" in caplog.text
        assert "x-other" not in caplog.text

    def test_header_keys_reach_the_grpc_exporter_lower_case(self, monkeypatch):
        """gRPC refuses an upper-case metadata key on every export
        ("Illegal header key"); the config's Authorization must not kill export."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
        settings = TelemetrySettings.from_config(
            {"endpoint": "http://127.0.0.1:4317", "headers": {"Authorization": "Bearer t"}})
        exporter = telemetry.build_span_exporter(settings)
        try:
            assert dict(exporter._headers) == {"authorization": "Bearer t"}
        finally:
            exporter.shutdown()


class TestExporters:

    def test_console_and_none(self):
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        assert isinstance(telemetry.build_span_exporter(TelemetrySettings(exporter="console")),
                          ConsoleSpanExporter)
        assert telemetry.build_span_exporter(TelemetrySettings(exporter="none")) is None

    def test_otlp_grpc_takes_the_configured_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = telemetry.build_span_exporter(
            TelemetrySettings(endpoint="http://collector.test:4317", insecure=True))
        try:
            assert isinstance(exporter, OTLPSpanExporter)
            assert exporter._endpoint == "collector.test:4317"
        finally:
            exporter.shutdown()

    def test_the_protocol_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
        assert TelemetrySettings().resolved_protocol("traces") == "http/protobuf"
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc")
        assert TelemetrySettings().resolved_protocol("traces") == "grpc"
        assert TelemetrySettings(protocol="http/protobuf").resolved_protocol("traces") == "http/protobuf"

    def test_a_missing_exporter_package_is_named_and_exports_nothing(self, monkeypatch, caplog):
        # None in sys.modules makes the import fail, installed or not.
        monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)
        with caplog.at_level(logging.ERROR):
            exporter = telemetry.build_span_exporter(TelemetrySettings(protocol="http/protobuf"))
        assert exporter is None
        assert "opentelemetry-exporter-otlp-proto-http" in caplog.text

    def test_the_service_name(self, monkeypatch):
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        monkeypatch.delenv("OTEL_RESOURCE_ATTRIBUTES", raising=False)
        name = "service.name"
        assert telemetry._resource(TelemetrySettings()).attributes[name] == "scarabhive"
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-env")
        assert telemetry._resource(TelemetrySettings()).attributes[name] == "from-env"
        assert telemetry._resource(TelemetrySettings(service_name="from-config")).attributes[name] \
            == "from-config"
        monkeypatch.delenv("OTEL_SERVICE_NAME")
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=from-resource-attributes")
        assert telemetry._resource(TelemetrySettings()).attributes[name] == "from-resource-attributes"


class TestWithoutOpenTelemetry:

    async def test_the_plugin_is_inert_and_says_why(self, monkeypatch, caplog):
        monkeypatch.setattr(otel_hooks, "trace_api", None)
        plugin = _plugin()
        with caplog.at_level(logging.ERROR):
            await plugin.start_plugin()
            result = await plugin.record_llm_call(HookContext(
                hook_type=HookType.POST_LLM_RESPONSE, request_id="r", session_id="s"))
        assert result.success is True
        assert plugin._pipeline is None and not plugin._runs
        assert "tracing is off" in caplog.text and "opentelemetry-sdk" in caplog.text
        await plugin.stop_plugin()
