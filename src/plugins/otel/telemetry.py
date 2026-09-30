"""The OpenTelemetry side of the plugin: exporters and providers.

Nothing in here knows about hooks. ``TelemetryPipeline`` hands out a tracer
(and, when metrics are on, the two GenAI instruments) and owns what it built:
the span processor, the exporters, their background threads.

Global state is left alone. When the host process already installed a
TracerProvider (an application embedding ScarabHive with its own tracing),
the pipeline borrows it and adds nothing -- the spans join the host's
pipeline and the plugin's exporter settings do not apply. Otherwise it builds
a private provider and never installs it globally: ``set_tracer_provider``
works once per process and cannot be undone, so a plugin that is stopped and
started again (or a second instance) would find its own dead provider there.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from opentelemetry import metrics as metrics_api
from opentelemetry import trace as trace_api

from .settings import TelemetrySettings

logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "scarabhive.otel"
INSTRUMENTATION_VERSION = "0.1.0"
DEFAULT_SERVICE_NAME = "scarabhive"


def _missing(package: str, what: str) -> None:
    logger.error("otel: %s needs the package %s, which is not installed -- nothing is "
                 "exported (pip install %s)", what, package, package)


def _otlp_kwargs(settings: TelemetrySettings, protocol: str) -> Dict[str, Any]:
    """Only what the config sets: everything left out the exporter reads from
    the standard OTEL_EXPORTER_OTLP_* variables itself."""
    kwargs: Dict[str, Any] = {}
    if settings.endpoint:
        kwargs["endpoint"] = settings.endpoint
    if settings.headers:
        kwargs["headers"] = dict(settings.headers)
    if settings.insecure is not None and protocol == "grpc":
        kwargs["insecure"] = settings.insecure
    return kwargs


def build_span_exporter(settings: TelemetrySettings) -> Optional[Any]:
    """The span exporter the config names, or None (``exporter: none``, or
    the package for it is missing -- said once, at ERROR)."""
    if settings.exporter == "none":
        return None
    if settings.exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        return ConsoleSpanExporter()
    protocol = settings.resolved_protocol("traces")
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError:
            _missing("opentelemetry-exporter-otlp-proto-grpc", "OTLP/gRPC span export")
            return None
        return OTLPSpanExporter(**_otlp_kwargs(settings, protocol))
    if protocol == "http/protobuf":
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter as OTLPHttpSpanExporter)
        except ImportError:
            _missing("opentelemetry-exporter-otlp-proto-http", "OTLP/HTTP span export")
            return None
        return OTLPHttpSpanExporter(**_otlp_kwargs(settings, protocol))
    logger.error("otel: OTLP protocol %r is not supported (grpc, http/protobuf) -- nothing is "
                 "exported", protocol)
    return None


def build_metric_reader(settings: TelemetrySettings) -> Optional[Any]:
    """A periodic reader around the metric exporter the config names, or None."""
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    exporter: Any = None
    if settings.exporter == "console":
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        exporter = ConsoleMetricExporter()
    elif settings.exporter == "otlp":
        protocol = settings.resolved_protocol("metrics")
        if protocol == "grpc":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            except ImportError:
                _missing("opentelemetry-exporter-otlp-proto-grpc", "OTLP/gRPC metric export")
                return None
            exporter = OTLPMetricExporter(**_otlp_kwargs(settings, protocol))
        elif protocol == "http/protobuf":
            try:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter as OTLPHttpMetricExporter)
            except ImportError:
                _missing("opentelemetry-exporter-otlp-proto-http", "OTLP/HTTP metric export")
                return None
            exporter = OTLPHttpMetricExporter(**_otlp_kwargs(settings, protocol))
        else:
            logger.error("otel: OTLP protocol %r is not supported for metrics", protocol)
            return None
    if exporter is None:
        return None
    return PeriodicExportingMetricReader(
        exporter, export_interval_millis=settings.metrics_export_interval_seconds * 1000)


def _resource(settings: TelemetrySettings) -> Any:
    """service.name: the config's, else what the environment says
    (OTEL_SERVICE_NAME, then OTEL_RESOURCE_ATTRIBUTES -- Resource.create reads
    both), else "scarabhive" instead of the SDK's unknown_service."""
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    resource = Resource.create({SERVICE_NAME: settings.service_name} if settings.service_name else {})
    if str(resource.attributes.get(SERVICE_NAME, "")).startswith("unknown_service"):
        resource = resource.merge(Resource({SERVICE_NAME: DEFAULT_SERVICE_NAME}))
    return resource


def host_tracer_provider() -> Optional[Any]:
    """The TracerProvider the host process installed, None if it installed none."""
    provider = trace_api.get_tracer_provider()
    if isinstance(provider, (trace_api.ProxyTracerProvider, trace_api.NoOpTracerProvider)):
        return None
    return provider


def host_meter_provider() -> Optional[Any]:
    """The MeterProvider the host process installed, None if it installed none.
    The API's placeholder class is private; it is recognized by name."""
    provider = metrics_api.get_meter_provider()
    if isinstance(provider, metrics_api.NoOpMeterProvider) or type(provider).__name__ == "_ProxyMeterProvider":
        return None
    return provider


class TelemetryPipeline:
    """Tracer and instruments for the hooks, and the resources behind them."""

    def __init__(self, settings: TelemetrySettings):
        self.settings = settings
        self.tracer: Any = None
        self.token_usage: Any = None
        self.operation_duration: Any = None
        self._tracer_provider: Any = None
        self._owns_tracer_provider = False
        self._meter_provider: Any = None
        self._owns_meter_provider = False

    @classmethod
    def create(cls, settings: TelemetrySettings) -> "TelemetryPipeline":
        pipeline = cls(settings)
        pipeline._build_traces()
        if settings.metrics:
            pipeline._build_metrics()
        return pipeline

    def _build_traces(self) -> None:
        host = host_tracer_provider()
        if host is not None:
            logger.info("otel: the process already has a TracerProvider (%s); spans go to it, "
                        "the plugin's exporter settings do not apply", type(host).__name__)
            self._tracer_provider = host
        else:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=_resource(self.settings))
            exporter = build_span_exporter(self.settings)
            if exporter is not None:
                provider.add_span_processor(BatchSpanProcessor(exporter))
            self._tracer_provider = provider
            self._owns_tracer_provider = True
        self.tracer = self._tracer_provider.get_tracer(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)

    def _build_metrics(self) -> None:
        host = host_meter_provider()
        if host is not None:
            self._meter_provider = host
        else:
            reader = build_metric_reader(self.settings)
            if reader is None:
                return
            from opentelemetry.sdk.metrics import MeterProvider

            self._meter_provider = MeterProvider(resource=_resource(self.settings),
                                                 metric_readers=[reader])
            self._owns_meter_provider = True
        meter = self._meter_provider.get_meter(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)
        # Names and units of the GenAI semantic conventions (gen_ai.client.*).
        self.token_usage = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}",
            description="Number of input and output tokens used.")
        self.operation_duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s",
            description="GenAI operation duration.")

    def force_flush(self, timeout_millis: int = 30000) -> None:
        for provider in (self._tracer_provider, self._meter_provider):
            flush = getattr(provider, "force_flush", None)
            if flush is not None:
                flush(timeout_millis)

    def _shutdown_blocking(self) -> None:
        """Flush what is buffered; shut down only what this pipeline built."""
        timeout_millis = int(self.settings.shutdown_timeout_seconds * 1000)
        for provider, owned in ((self._tracer_provider, self._owns_tracer_provider),
                                (self._meter_provider, self._owns_meter_provider)):
            if provider is None:
                continue
            try:
                if owned:
                    provider.shutdown()
                else:
                    flush = getattr(provider, "force_flush", None)
                    if flush is not None:
                        flush(timeout_millis)
            except Exception as error:  # noqa: BLE001 - stopping must never raise
                logger.warning("otel: shutting down %s failed: %s", type(provider).__name__, error)

    async def shutdown(self) -> None:
        """Flush and shut down off the event loop, waiting at most
        ``shutdown_timeout_seconds``: an unreachable collector must not hold
        up the process that stops. The thread is a daemon and finishes (or
        dies with the process) on its own."""
        worker = threading.Thread(target=self._shutdown_blocking, name="otel-shutdown", daemon=True)
        worker.start()
        deadline = time.monotonic() + self.settings.shutdown_timeout_seconds
        while worker.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if worker.is_alive():
            logger.warning("otel: flushing did not finish within %.1fs; spans still buffered "
                           "may be lost", self.settings.shutdown_timeout_seconds)
