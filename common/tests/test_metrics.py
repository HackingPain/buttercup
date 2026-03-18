"""Unit tests for the lightweight Prometheus metrics module."""

from __future__ import annotations

import threading
import time

from buttercup.common.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    _format_labels,
    _label_key,
)

# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestLabelKey:
    def test_empty(self) -> None:
        assert _label_key({}) == ()

    def test_sorted(self) -> None:
        result = _label_key({"z": "1", "a": "2"})
        assert result == (("a", "2"), ("z", "1"))


class TestFormatLabels:
    def test_empty(self) -> None:
        assert _format_labels({}) == ""

    def test_single(self) -> None:
        assert _format_labels({"method": "GET"}) == '{method="GET"}'

    def test_multiple_sorted(self) -> None:
        result = _format_labels({"status": "200", "method": "POST"})
        assert result == '{method="POST",status="200"}'


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------


class TestCounter:
    def test_inc_default(self) -> None:
        c = Counter("test_total", "A test counter.")
        c.inc(method="GET")
        c.inc(method="GET")
        lines = c.collect()
        assert "# TYPE test_total counter" in lines
        assert 'test_total{method="GET"} 2' in lines

    def test_inc_custom_value(self) -> None:
        c = Counter("req_total", "Requests.")
        c.inc(5, method="POST")
        lines = c.collect()
        assert 'req_total{method="POST"} 5' in lines

    def test_multiple_label_sets(self) -> None:
        c = Counter("multi", "Multi-label counter.")
        c.inc(method="GET", endpoint="/a")
        c.inc(method="POST", endpoint="/b")
        c.inc(method="GET", endpoint="/a")
        output = "\n".join(c.collect())
        assert 'multi{endpoint="/a",method="GET"} 2' in output
        assert 'multi{endpoint="/b",method="POST"} 1' in output

    def test_no_labels(self) -> None:
        c = Counter("plain", "No labels.")
        c.inc()
        c.inc()
        lines = c.collect()
        assert "plain 2" in lines

    def test_thread_safety(self) -> None:
        c = Counter("ts_counter", "Thread-safe counter.")
        n = 1000

        def bump() -> None:
            for _ in range(n):
                c.inc(worker="w")

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = c.collect()
        assert f'ts_counter{{worker="w"}} {4 * n}' in lines


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------


class TestGauge:
    def test_set(self) -> None:
        g = Gauge("depth", "Queue depth.")
        g.set(42, queue="q1")
        lines = g.collect()
        assert 'depth{queue="q1"} 42' in lines

    def test_inc_dec(self) -> None:
        g = Gauge("g", "g")
        g.inc(5)
        g.dec(2)
        lines = g.collect()
        assert "g 3" in lines

    def test_overwrite(self) -> None:
        g = Gauge("g2", "g2")
        g.set(10, name="a")
        g.set(20, name="a")
        lines = g.collect()
        assert 'g2{name="a"} 20' in lines


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


class TestHistogram:
    def test_observe(self) -> None:
        h = Histogram("dur", "Duration.", buckets=(0.1, 0.5, 1.0))
        h.observe(0.05)
        h.observe(0.3)
        h.observe(0.8)
        h.observe(2.0)
        output = "\n".join(h.collect())
        # Cumulative bucket counts: <=0.1: 1, <=0.5: 2, <=1.0: 3, <=+Inf: 4
        assert 'dur_bucket{le="0.1"} 1' in output
        assert 'dur_bucket{le="0.5"} 2' in output
        assert 'dur_bucket{le="1"} 3' in output
        assert 'dur_bucket{le="+Inf"} 4' in output
        assert "dur_count 4" in output

    def test_observe_with_labels(self) -> None:
        h = Histogram("lat", "Latency.", buckets=(1.0,))
        h.observe(0.5, endpoint="/a")
        h.observe(1.5, endpoint="/a")
        output = "\n".join(h.collect())
        assert 'lat_bucket{endpoint="/a",le="1"} 1' in output
        assert 'lat_bucket{endpoint="/a",le="+Inf"} 2' in output
        assert 'lat_count{endpoint="/a"} 2' in output

    def test_timer_context_manager(self) -> None:
        h = Histogram("timer", "Timer.", buckets=(0.5, 1.0))
        with h.time(op="test"):
            time.sleep(0.01)
        output = "\n".join(h.collect())
        assert "timer_count" in output
        assert "timer_sum" in output

    def test_inf_bucket_always_present(self) -> None:
        h = Histogram("no_inf", "No inf.", buckets=(0.1, 0.5))
        h.observe(0.01)
        output = "\n".join(h.collect())
        assert 'le="+Inf"' in output


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestMetricsRegistry:
    def test_collect_empty(self) -> None:
        r = MetricsRegistry()
        r.counter("c", "c")
        output = r.collect()
        # Should contain HELP and TYPE even with no data points
        assert "# HELP c c" in output
        assert "# TYPE c counter" in output

    def test_collect_multiple_metrics(self) -> None:
        r = MetricsRegistry()
        c = r.counter("requests", "Total requests.")
        g = r.gauge("active", "Active connections.")
        h = r.histogram("latency", "Latency.", buckets=(1.0,))

        c.inc(method="GET")
        g.set(5)
        h.observe(0.42)

        output = r.collect()
        assert "# TYPE requests counter" in output
        assert "# TYPE active gauge" in output
        assert "# TYPE latency histogram" in output
        assert 'requests{method="GET"} 1' in output
        assert "active 5" in output
        assert "latency_count 1" in output

    def test_output_ends_with_newline(self) -> None:
        r = MetricsRegistry()
        r.counter("c", "c")
        assert r.collect().endswith("\n")
