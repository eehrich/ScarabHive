"""The plugin's config block, checked once -- no OpenTelemetry import here,
so a plugin whose packages are missing still loads and says so."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_EXPORTERS = ("otlp", "console", "none")
_PROTOCOLS = ("grpc", "http/protobuf")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off"):
        return False
    return default


def _as_number(value: Any, default: float, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


@dataclass(frozen=True)
class TelemetrySettings:
    """The plugin's config block, checked once. A value that does not fit
    falls back to its default with a warning -- the framework validates no
    plugin config, and a typo must not switch content capture on."""

    exporter: str = "otlp"
    protocol: str = ""
    endpoint: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    insecure: Optional[bool] = None
    service_name: str = ""
    capture_content: bool = False
    content_max_chars: int = 2000
    capture_user_id: bool = False
    max_open_runs: int = 1000
    max_pending_tool_calls: int = 4096
    idle_timeout_seconds: float = 21600.0
    metrics: bool = False
    metrics_export_interval_seconds: float = 60.0
    shutdown_timeout_seconds: float = 5.0

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "TelemetrySettings":
        defaults = cls()
        exporter = str(config.get("exporter") or defaults.exporter).strip().lower()
        if exporter not in _EXPORTERS:
            logger.warning("otel: exporter %r is not one of %s; using %r",
                           exporter, list(_EXPORTERS), defaults.exporter)
            exporter = defaults.exporter
        protocol = str(config.get("protocol") or "").strip().lower()
        if protocol and protocol not in _PROTOCOLS:
            logger.warning("otel: protocol %r is not one of %s; using the environment's or grpc",
                           protocol, list(_PROTOCOLS))
            protocol = ""
        headers = config.get("headers") or {}
        if not isinstance(headers, dict):
            logger.warning("otel: headers must be a mapping; ignored")
            headers = {}
        insecure = config.get("insecure")
        return cls(
            exporter=exporter,
            protocol=protocol,
            endpoint=str(config.get("endpoint") or "").strip(),
            headers={str(k): str(v) for k, v in headers.items() if v not in (None, "")},
            insecure=None if insecure in (None, "") else _as_bool(insecure, False),
            service_name=str(config.get("service_name") or "").strip(),
            capture_content=_as_bool(config.get("capture_content"), defaults.capture_content),
            content_max_chars=int(_as_number(config.get("content_max_chars"),
                                             defaults.content_max_chars, 16)),
            capture_user_id=_as_bool(config.get("capture_user_id"), defaults.capture_user_id),
            max_open_runs=int(_as_number(config.get("max_open_runs"), defaults.max_open_runs, 1)),
            max_pending_tool_calls=int(_as_number(config.get("max_pending_tool_calls"),
                                                  defaults.max_pending_tool_calls, 1)),
            idle_timeout_seconds=_as_number(config.get("idle_timeout_seconds"),
                                            defaults.idle_timeout_seconds, 1.0),
            metrics=_as_bool(config.get("metrics"), defaults.metrics),
            metrics_export_interval_seconds=_as_number(
                config.get("metrics_export_interval_seconds"),
                defaults.metrics_export_interval_seconds, 1.0),
            shutdown_timeout_seconds=_as_number(config.get("shutdown_timeout_seconds"),
                                                defaults.shutdown_timeout_seconds, 0.1),
        )

    def resolved_protocol(self, signal: str) -> str:
        """The OTLP protocol: the config's, else the standard environment
        variables (per signal first), else grpc."""
        if self.protocol:
            return self.protocol
        for name in (f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL", "OTEL_EXPORTER_OTLP_PROTOCOL"):
            value = os.environ.get(name, "").strip().lower()
            if value:
                return value
        return "grpc"
