"""Prometheus metrics with bounded-cardinality labels."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
    platform_collector,
)
from prometheus_client.gc_collector import GCCollector


def get_metrics() -> Metrics:
    raise RuntimeError("Metrics dependency is not configured")


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        platform_collector.PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self.http_requests = Counter(
            "morphlake_http_requests_total",
            "HTTP requests",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "morphlake_http_request_duration_seconds",
            "HTTP request duration",
            ["method", "route"],
            registry=self.registry,
        )
        self.transfer_requests = Counter(
            "morphlake_transfer_requests_total",
            "Upload and download requests",
            ["operation", "business_domain", "department", "status"],
            registry=self.registry,
        )
        self.transfer_bytes = Counter(
            "morphlake_transfer_bytes_total",
            "Uploaded and downloaded bytes",
            ["operation", "business_domain", "department", "status"],
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "morphlake_auth_failures_total",
            "Rejected API token attempts",
            ["reason"],
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "morphlake_rate_limited_total",
            "Rate-limited transfer requests",
            ["operation", "business_domain", "department"],
            registry=self.registry,
        )
        self.component_up = Gauge(
            "morphlake_component_up",
            "Component connectivity status",
            ["component"],
            registry=self.registry,
        )
        self.index_runs = Counter(
            "morphlake_index_maintenance_total",
            "Paimon index maintenance runs",
            ["status"],
            registry=self.registry,
        )
        self.index_duration = Histogram(
            "morphlake_index_maintenance_duration_seconds",
            "Paimon index maintenance duration",
            registry=self.registry,
        )
        self.index_last_success = Gauge(
            "morphlake_index_last_success_timestamp_seconds",
            "Unix timestamp of latest successful index maintenance",
            registry=self.registry,
        )
        self.audit_backlog = Gauge(
            "morphlake_transfer_audit_backlog",
            "Transfer events waiting for Paimon flush",
            registry=self.registry,
        )
        self.audit_flush = Counter(
            "morphlake_transfer_audit_flush_total",
            "Transfer audit flush operations",
            ["status"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def observe_transfer(
        self,
        *,
        operation: str,
        business_domain: str,
        department: str,
        status: str,
        byte_count: int,
    ) -> None:
        labels = (operation, business_domain, department, status)
        self.transfer_requests.labels(*labels).inc()
        self.transfer_bytes.labels(*labels).inc(max(0, byte_count))
