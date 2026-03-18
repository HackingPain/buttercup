"""Lightweight Prometheus-compatible metrics registry.

Provides Counter, Gauge, and Histogram metric types with a text-format
exporter that outputs the Prometheus exposition format.  No external
dependencies are required -- this module is entirely self-contained.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Return a hashable, sorted representation of a label dict."""
    return tuple(sorted(labels.items()))


def _format_labels(labels: dict[str, str]) -> str:
    """Format labels into Prometheus exposition format."""
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


# ---------------------------------------------------------------------------
# Metric types
# ---------------------------------------------------------------------------


class Counter:
    """A monotonically increasing counter."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] += value

    def collect(self) -> list[str]:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            for lk, val in sorted(self._values.items()):
                lbl = _format_labels(dict(lk))
                lines.append(f"{self.name}{lbl} {val:g}")
        return lines


class Gauge:
    """A value that can go up and down."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] -= value

    def collect(self) -> list[str]:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
        ]
        with self._lock:
            for lk, val in sorted(self._values.items()):
                lbl = _format_labels(dict(lk))
                lines.append(f"{self.name}{lbl} {val:g}")
        return lines


# Default histogram bucket boundaries (matches Prometheus client defaults).
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
    float("inf"),
)


@dataclass
class _HistogramData:
    bucket_counts: list[int] = field(default_factory=list)
    sum_value: float = 0.0
    count: int = 0


class Histogram:
    """A histogram that tracks the distribution of observed values."""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        # Ensure +Inf is always the last bucket.
        if not buckets or buckets[-1] != float("inf"):
            buckets = (*buckets, float("inf"))
        self.buckets = buckets
        self._data: dict[tuple[tuple[str, str], ...], _HistogramData] = {}
        self._lock = threading.Lock()

    def _get_data(self, key: tuple[tuple[str, str], ...]) -> _HistogramData:
        if key not in self._data:
            self._data[key] = _HistogramData(bucket_counts=[0] * len(self.buckets))
        return self._data[key]

    def observe(self, value: float, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            data = self._get_data(key)
            data.sum_value += value
            data.count += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    data.bucket_counts[i] += 1
                    break

    def time(self, **labels: str) -> _HistogramTimer:
        """Return a context manager that observes elapsed seconds."""
        return _HistogramTimer(self, labels)

    def collect(self) -> list[str]:
        lines: list[str] = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            for lk, data in sorted(self._data.items()):
                lbl_dict = dict(lk)
                cumulative = 0
                for i, bound in enumerate(self.buckets):
                    cumulative += data.bucket_counts[i]
                    le_labels = {**lbl_dict, "le": "+Inf" if math.isinf(bound) else f"{bound:g}"}
                    lbl = _format_labels(le_labels)
                    lines.append(f"{self.name}_bucket{lbl} {cumulative}")
                lbl = _format_labels(lbl_dict)
                lines.append(f"{self.name}_count{lbl} {data.count}")
                lines.append(f"{self.name}_sum{lbl} {data.sum_value:g}")
        return lines


class _HistogramTimer:
    def __init__(self, histogram: Histogram, labels: dict[str, str]) -> None:
        self._histogram = histogram
        self._labels = labels
        self._start: float = 0.0

    def __enter__(self) -> _HistogramTimer:
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        elapsed = time.monotonic() - self._start
        self._histogram.observe(elapsed, **self._labels)


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------


class MetricsRegistry:
    """Central registry that owns all application metrics."""

    def __init__(self) -> None:
        self._collectors: list[Counter | Gauge | Histogram] = []

    def counter(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Counter:
        c = Counter(name, help_text, label_names)
        self._collectors.append(c)
        return c

    def gauge(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Gauge:
        g = Gauge(name, help_text, label_names)
        self._collectors.append(g)
        return g

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> Histogram:
        h = Histogram(name, help_text, label_names, buckets)
        self._collectors.append(h)
        return h

    def collect(self) -> str:
        """Return all metrics in Prometheus text exposition format."""
        blocks: list[str] = []
        for collector in self._collectors:
            lines = collector.collect()
            if lines:
                blocks.append("\n".join(lines))
        return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Singleton registry & pre-defined application metrics
# ---------------------------------------------------------------------------

REGISTRY = MetricsRegistry()

HTTP_REQUESTS_TOTAL = REGISTRY.counter(
    "crs_http_requests_total",
    "Total number of HTTP requests.",
    label_names=("method", "endpoint", "status_code"),
)

HTTP_REQUEST_DURATION = REGISTRY.histogram(
    "crs_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    label_names=("method", "endpoint"),
)

LLM_CALLS_TOTAL = REGISTRY.counter(
    "crs_llm_calls_total",
    "Total number of LLM API calls.",
    label_names=("function", "status"),
)

LLM_CALL_DURATION = REGISTRY.histogram(
    "crs_llm_call_duration_seconds",
    "LLM API call duration in seconds.",
    label_names=("function",),
)

QUEUE_DEPTH = REGISTRY.gauge(
    "crs_queue_depth",
    "Current depth of Redis-backed reliable queues.",
    label_names=("queue_name",),
)
