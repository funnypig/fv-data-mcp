from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from fv_data_mcp.auth import request_context
from fv_data_mcp.config import Settings


class JsonFormatter(logging.Formatter):
    """Emit bounded JSON logs while redacting secrets and omitting raw exceptions."""

    secrets: list[str]

    def __init__(self, secrets: list[str]) -> None:
        """Record configured secrets to redact from emitted log messages."""
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        """Serialize safe event fields and the current request/trace correlation IDs."""
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"(?i)(auth|token|password|secret)=([^&\s]+)", r"\1=[REDACTED]", message)
        message = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [REDACTED]", message)
        result = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": message[:2000],
            "request_id": request_context.get(),
        }
        for field in ("tool", "duration_ms", "error_code", "dataset", "status", "cache_hit"):
            if hasattr(record, field):
                result[field] = getattr(record, field)
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            result["trace_id"] = format(span.trace_id, "032x")
        return json.dumps(result)


def configure(settings: Settings) -> list[TracerProvider | MeterProvider]:
    """Install redacted logging and optional Azure OpenTelemetry exporters.

    Args:
        settings: Logging secrets, environment metadata, and exporter configuration.

    Returns:
        Export providers the caller must shut down when the process exits.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            [
                settings.finviz_api_key.get_secret_value(),
                settings.redis_url.get_secret_value(),
                settings.dev_token.get_secret_value(),
                settings.cursor_secret.get_secret_value(),
                settings.applicationinsights_connection_string.get_secret_value(),
            ]
        )
    )
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ("httpx", "httpcore", "azure", "mcp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    providers = []
    connection = settings.applicationinsights_connection_string.get_secret_value()
    if connection:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )

        resource = Resource.create(
            {
                "service.name": "fv-data-mcp",
                "service.version": "0.1.0",
                "deployment.environment.name": settings.environment,
            }
        )
        traces = TracerProvider(
            resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio))
        )
        traces.add_span_processor(
            BatchSpanProcessor(
                AzureMonitorTraceExporter(
                    connection_string=connection, disable_offline_storage=True
                )
            )
        )
        trace.set_tracer_provider(traces)
        meter = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    AzureMonitorMetricExporter(
                        connection_string=connection, disable_offline_storage=True
                    )
                )
            ],
        )
        metrics.set_meter_provider(meter)
        providers.extend([traces, meter])
    return providers
