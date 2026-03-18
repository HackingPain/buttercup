"""Tests for the patcher timeout guardrails."""

from __future__ import annotations

import logging
import time

import pytest

from buttercup.patcher.timeouts import PatcherTimeoutConfig, PatcherTimeoutError, TimeoutGuard

# ---------------------------------------------------------------------------
# PatcherTimeoutConfig
# ---------------------------------------------------------------------------


class TestPatcherTimeoutConfig:
    def test_defaults(self) -> None:
        cfg = PatcherTimeoutConfig()
        assert cfg.total_workflow_timeout_seconds == 1800
        assert cfg.per_agent_timeout_seconds == 300
        assert cfg.max_reflection_loops == 5
        assert cfg.max_total_llm_calls == 50

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BUTTERCUP_PATCHER_TOTAL_WORKFLOW_TIMEOUT_SECONDS", "600")
        monkeypatch.setenv("BUTTERCUP_PATCHER_PER_AGENT_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("BUTTERCUP_PATCHER_MAX_REFLECTION_LOOPS", "3")
        monkeypatch.setenv("BUTTERCUP_PATCHER_MAX_TOTAL_LLM_CALLS", "20")
        cfg = PatcherTimeoutConfig()
        assert cfg.total_workflow_timeout_seconds == 600
        assert cfg.per_agent_timeout_seconds == 120
        assert cfg.max_reflection_loops == 3
        assert cfg.max_total_llm_calls == 20


# ---------------------------------------------------------------------------
# TimeoutGuard — workflow timeout
# ---------------------------------------------------------------------------


class TestWorkflowTimeout:
    def test_no_error_before_limit(self) -> None:
        cfg = PatcherTimeoutConfig(total_workflow_timeout_seconds=10)
        guard = TimeoutGuard(cfg)
        guard.start()
        guard.check_limits()  # should not raise

    def test_raises_after_limit(self) -> None:
        cfg = PatcherTimeoutConfig(total_workflow_timeout_seconds=1)
        guard = TimeoutGuard(cfg)
        guard.start()
        # Simulate elapsed time by backdating the start
        guard._workflow_start = time.monotonic() - 2
        with pytest.raises(PatcherTimeoutError, match="Total workflow timeout exceeded"):
            guard.check_limits()

    def test_warning_at_80_percent(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PatcherTimeoutConfig(total_workflow_timeout_seconds=100)
        guard = TimeoutGuard(cfg)
        guard.start()
        # Set elapsed to 81 seconds
        guard._workflow_start = time.monotonic() - 81
        with caplog.at_level(logging.WARNING):
            # Should not raise (81 < 100) but should warn
            try:
                guard.check_limits()
            except PatcherTimeoutError:
                pass  # in case of rounding
        assert any("Approaching total workflow timeout" in rec.message for rec in caplog.records)

    def test_warning_only_once(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PatcherTimeoutConfig(total_workflow_timeout_seconds=100)
        guard = TimeoutGuard(cfg)
        guard.start()
        guard._workflow_start = time.monotonic() - 81
        with caplog.at_level(logging.WARNING):
            try:
                guard.check_limits()
            except PatcherTimeoutError:
                pass
            caplog.clear()
            try:
                guard.check_limits()
            except PatcherTimeoutError:
                pass
        assert not any("Approaching total workflow timeout" in rec.message for rec in caplog.records)

    def test_no_error_when_not_started(self) -> None:
        guard = TimeoutGuard()
        guard.check_limits()  # should not raise — timer not started


# ---------------------------------------------------------------------------
# TimeoutGuard — per-agent timeout
# ---------------------------------------------------------------------------


class TestAgentTimeout:
    def test_no_error_inside_limit(self) -> None:
        cfg = PatcherTimeoutConfig(per_agent_timeout_seconds=10)
        guard = TimeoutGuard(cfg)
        guard.start()
        with guard.agent_scope("swe"):
            guard.check_limits()

    def test_raises_after_agent_limit(self) -> None:
        cfg = PatcherTimeoutConfig(per_agent_timeout_seconds=1)
        guard = TimeoutGuard(cfg)
        guard.start()
        with guard.agent_scope("swe"):
            guard._agent_start = time.monotonic() - 2
            with pytest.raises(PatcherTimeoutError, match="Per-agent timeout exceeded.*swe"):
                guard.check_limits()

    def test_agent_scope_cleans_up(self) -> None:
        guard = TimeoutGuard()
        guard.start()
        with guard.agent_scope("swe"):
            assert guard._current_agent == "swe"
        assert guard._current_agent is None
        assert guard._agent_start is None

    def test_agent_scope_cleans_up_on_exception(self) -> None:
        guard = TimeoutGuard()
        guard.start()
        with pytest.raises(RuntimeError):
            with guard.agent_scope("swe"):
                raise RuntimeError("boom")
        assert guard._current_agent is None

    def test_agent_warning_at_80_percent(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PatcherTimeoutConfig(per_agent_timeout_seconds=100)
        guard = TimeoutGuard(cfg)
        guard.start()
        with guard.agent_scope("reflection"):
            guard._agent_start = time.monotonic() - 81
            with caplog.at_level(logging.WARNING):
                try:
                    guard.check_limits()
                except PatcherTimeoutError:
                    pass
        assert any("Approaching per-agent timeout" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# TimeoutGuard — reflection loops
# ---------------------------------------------------------------------------


class TestReflectionLoops:
    def test_no_error_below_limit(self) -> None:
        cfg = PatcherTimeoutConfig(max_reflection_loops=3)
        guard = TimeoutGuard(cfg)
        guard.start()
        guard.record_reflection_loop()
        guard.record_reflection_loop()
        guard.check_limits()  # 2 < 3

    def test_raises_at_limit(self) -> None:
        cfg = PatcherTimeoutConfig(max_reflection_loops=2)
        guard = TimeoutGuard(cfg)
        guard.start()
        guard.record_reflection_loop()
        guard.record_reflection_loop()
        with pytest.raises(PatcherTimeoutError, match="Max reflection loops exceeded"):
            guard.check_limits()

    def test_warning_at_80_percent(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PatcherTimeoutConfig(max_reflection_loops=5)
        guard = TimeoutGuard(cfg)
        guard.start()
        for _ in range(4):
            guard.record_reflection_loop()
        with caplog.at_level(logging.WARNING):
            guard.check_limits()
        assert any("Approaching max reflection loops" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# TimeoutGuard — LLM calls
# ---------------------------------------------------------------------------


class TestLLMCalls:
    def test_no_error_below_limit(self) -> None:
        cfg = PatcherTimeoutConfig(max_total_llm_calls=5)
        guard = TimeoutGuard(cfg)
        guard.start()
        for _ in range(4):
            guard.record_llm_call()
        guard.check_limits()

    def test_raises_at_limit(self) -> None:
        cfg = PatcherTimeoutConfig(max_total_llm_calls=3)
        guard = TimeoutGuard(cfg)
        guard.start()
        for _ in range(3):
            guard.record_llm_call()
        with pytest.raises(PatcherTimeoutError, match="Max total LLM calls exceeded"):
            guard.check_limits()

    def test_warning_at_80_percent(self, caplog: pytest.LogCaptureFixture) -> None:
        cfg = PatcherTimeoutConfig(max_total_llm_calls=10)
        guard = TimeoutGuard(cfg)
        guard.start()
        for _ in range(8):
            guard.record_llm_call()
        with caplog.at_level(logging.WARNING):
            guard.check_limits()
        assert any("Approaching max total LLM calls" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# TimeoutGuard — accessors
# ---------------------------------------------------------------------------


class TestAccessors:
    def test_elapsed_workflow_seconds_not_started(self) -> None:
        guard = TimeoutGuard()
        assert guard.elapsed_workflow_seconds == 0.0

    def test_elapsed_workflow_seconds_after_start(self) -> None:
        guard = TimeoutGuard()
        guard.start()
        assert guard.elapsed_workflow_seconds >= 0.0

    def test_elapsed_agent_seconds_outside_scope(self) -> None:
        guard = TimeoutGuard()
        assert guard.elapsed_agent_seconds == 0.0

    def test_counters_initial(self) -> None:
        guard = TimeoutGuard()
        assert guard.reflection_loops == 0
        assert guard.total_llm_calls == 0

    def test_counters_increment(self) -> None:
        guard = TimeoutGuard()
        guard.record_llm_call()
        guard.record_llm_call()
        guard.record_reflection_loop()
        assert guard.total_llm_calls == 2
        assert guard.reflection_loops == 1


# ---------------------------------------------------------------------------
# Combined limits
# ---------------------------------------------------------------------------


class TestCombinedLimits:
    def test_first_exceeded_limit_wins(self) -> None:
        """When multiple limits are close, the first checked one triggers."""
        cfg = PatcherTimeoutConfig(
            total_workflow_timeout_seconds=1,
            max_reflection_loops=1,
            max_total_llm_calls=1,
        )
        guard = TimeoutGuard(cfg)
        guard.start()
        guard._workflow_start = time.monotonic() - 2
        guard.record_reflection_loop()
        guard.record_llm_call()
        # Workflow timeout is checked first
        with pytest.raises(PatcherTimeoutError, match="Total workflow timeout exceeded"):
            guard.check_limits()
