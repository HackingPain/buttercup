"""Timeout guardrails for the patcher's LangGraph multi-agent workflow.

Provides configurable limits for total workflow time, per-agent time,
reflection loop iterations, and total LLM calls. The TimeoutGuard class
can be checked at key points in the workflow to enforce these limits.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_WARNING_THRESHOLD = 0.8


class PatcherTimeoutError(Exception):
    """Raised when any timeout or iteration limit is exceeded."""


class PatcherTimeoutConfig(BaseSettings):
    """Configurable limits for patcher workflow execution.

    All values can be overridden via environment variables with the
    ``BUTTERCUP_PATCHER_`` prefix (e.g. ``BUTTERCUP_PATCHER_TOTAL_WORKFLOW_TIMEOUT_SECONDS``).
    """

    total_workflow_timeout_seconds: int = Field(
        default=1800,
        description="Maximum wall-clock seconds for the entire patching workflow (default 30 min)",
    )
    per_agent_timeout_seconds: int = Field(
        default=300,
        description="Maximum wall-clock seconds for a single agent invocation (default 5 min)",
    )
    max_reflection_loops: int = Field(
        default=5,
        description="Maximum number of reflection loop iterations",
    )
    max_total_llm_calls: int = Field(
        default=50,
        description="Maximum total LLM calls across the entire workflow",
    )

    model_config = SettingsConfigDict(
        env_prefix="BUTTERCUP_PATCHER_",
        env_file=".env",
        extra="ignore",
    )


class TimeoutGuard:
    """Tracks and enforces timeout / iteration limits for a patcher workflow run.

    Usage::

        guard = TimeoutGuard()          # uses default config
        guard.start()                   # start the overall timer

        with guard.agent_scope("swe"):  # start a per-agent timer
            guard.record_llm_call()
            guard.check_limits()        # raises PatcherTimeoutError if exceeded

        guard.record_reflection_loop()
        guard.check_limits()
    """

    def __init__(self, config: PatcherTimeoutConfig | None = None) -> None:
        self.config = config or PatcherTimeoutConfig()

        # Overall workflow tracking
        self._workflow_start: float | None = None

        # Per-agent tracking
        self._current_agent: str | None = None
        self._agent_start: float | None = None

        # Counters
        self._reflection_loops: int = 0
        self._total_llm_calls: int = 0

        # Warning flags (to avoid spamming)
        self._warned_workflow_time: bool = False
        self._warned_agent_time: bool = False
        self._warned_reflection_loops: bool = False
        self._warned_llm_calls: bool = False

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start (or restart) the overall workflow timer."""
        self._workflow_start = time.monotonic()

    @contextmanager
    def agent_scope(self, agent_name: str) -> Generator[None, None, None]:
        """Context manager that tracks per-agent elapsed time."""
        self._current_agent = agent_name
        self._agent_start = time.monotonic()
        self._warned_agent_time = False
        try:
            yield
        finally:
            self._current_agent = None
            self._agent_start = None

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def record_llm_call(self) -> None:
        """Increment the total LLM call counter."""
        with self._lock:
            self._total_llm_calls += 1

    def record_reflection_loop(self) -> None:
        """Increment the reflection loop counter."""
        with self._lock:
            self._reflection_loops += 1

    # ------------------------------------------------------------------
    # Read-only accessors
    # ------------------------------------------------------------------

    @property
    def elapsed_workflow_seconds(self) -> float:
        """Seconds elapsed since the workflow started."""
        if self._workflow_start is None:
            return 0.0
        return time.monotonic() - self._workflow_start

    @property
    def elapsed_agent_seconds(self) -> float:
        """Seconds elapsed since the current agent scope started."""
        if self._agent_start is None:
            return 0.0
        return time.monotonic() - self._agent_start

    @property
    def reflection_loops(self) -> int:
        return self._reflection_loops

    @property
    def total_llm_calls(self) -> int:
        return self._total_llm_calls

    # ------------------------------------------------------------------
    # Limit checking
    # ------------------------------------------------------------------

    def check_limits(self) -> None:
        """Check all limits and raise :class:`PatcherTimeoutError` if any are exceeded.

        Also logs a warning when any metric reaches 80 % of its limit.
        """
        self._check_workflow_time()
        self._check_agent_time()
        self._check_reflection_loops()
        self._check_llm_calls()

    # -- individual checks ------------------------------------------------

    def _check_workflow_time(self) -> None:
        if self._workflow_start is None:
            return
        elapsed = self.elapsed_workflow_seconds
        limit = self.config.total_workflow_timeout_seconds
        if elapsed >= limit:
            raise PatcherTimeoutError(f"Total workflow timeout exceeded: {elapsed:.0f}s >= {limit}s")
        if not self._warned_workflow_time and elapsed >= limit * _WARNING_THRESHOLD:
            logger.warning(
                "Approaching total workflow timeout: %.0f / %d seconds (%.0f%%)",
                elapsed,
                limit,
                elapsed / limit * 100,
            )
            self._warned_workflow_time = True

    def _check_agent_time(self) -> None:
        if self._agent_start is None:
            return
        elapsed = self.elapsed_agent_seconds
        limit = self.config.per_agent_timeout_seconds
        if elapsed >= limit:
            raise PatcherTimeoutError(
                f"Per-agent timeout exceeded for '{self._current_agent}': {elapsed:.0f}s >= {limit}s"
            )
        if not self._warned_agent_time and elapsed >= limit * _WARNING_THRESHOLD:
            logger.warning(
                "Approaching per-agent timeout for '%s': %.0f / %d seconds (%.0f%%)",
                self._current_agent,
                elapsed,
                limit,
                elapsed / limit * 100,
            )
            self._warned_agent_time = True

    def _check_reflection_loops(self) -> None:
        limit = self.config.max_reflection_loops
        if self._reflection_loops >= limit:
            raise PatcherTimeoutError(f"Max reflection loops exceeded: {self._reflection_loops} >= {limit}")
        if not self._warned_reflection_loops and self._reflection_loops >= limit * _WARNING_THRESHOLD:
            logger.warning(
                "Approaching max reflection loops: %d / %d (%.0f%%)",
                self._reflection_loops,
                limit,
                self._reflection_loops / limit * 100,
            )
            self._warned_reflection_loops = True

    def _check_llm_calls(self) -> None:
        limit = self.config.max_total_llm_calls
        if self._total_llm_calls >= limit:
            raise PatcherTimeoutError(f"Max total LLM calls exceeded: {self._total_llm_calls} >= {limit}")
        if not self._warned_llm_calls and self._total_llm_calls >= limit * _WARNING_THRESHOLD:
            logger.warning(
                "Approaching max total LLM calls: %d / %d (%.0f%%)",
                self._total_llm_calls,
                limit,
                self._total_llm_calls / limit * 100,
            )
            self._warned_llm_calls = True
