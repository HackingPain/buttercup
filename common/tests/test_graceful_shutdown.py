"""Tests for GracefulShutdown and graceful shutdown integration in serve loops."""

import asyncio
import signal
import threading
import time

import pytest

from buttercup.common.utils import (
    GracefulShutdown,
    async_serve_loop,
    get_shutdown_handler,
    is_shutting_down,
    serve_loop,
)


class TestGracefulShutdown:
    """Tests for the GracefulShutdown class."""

    def test_initial_state_not_shutting_down(self) -> None:
        gs = GracefulShutdown()
        assert gs.is_shutting_down is False

    def test_request_shutdown_sets_flag(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()
        assert gs.is_shutting_down is True

    def test_request_shutdown_idempotent(self) -> None:
        gs = GracefulShutdown()
        gs.request_shutdown()
        gs.request_shutdown()
        assert gs.is_shutting_down is True

    def test_context_manager(self) -> None:
        gs = GracefulShutdown()
        with gs as ctx:
            assert ctx is gs
            assert not ctx.is_shutting_down

    def test_signal_handler_sets_flag(self) -> None:
        gs = GracefulShutdown()
        gs._signal_handler(signal.SIGTERM, None)
        assert gs.is_shutting_down is True

    def test_install_and_restore_signal_handlers(self) -> None:
        gs = GracefulShutdown()
        original_sigterm = signal.getsignal(signal.SIGTERM)
        original_sigint = signal.getsignal(signal.SIGINT)

        gs.install_signal_handlers()
        # Handlers should now be the GracefulShutdown handler
        assert signal.getsignal(signal.SIGTERM) == gs._signal_handler
        assert signal.getsignal(signal.SIGINT) == gs._signal_handler

        gs.restore_signal_handlers()
        # Handlers should be restored
        assert signal.getsignal(signal.SIGTERM) == original_sigterm
        assert signal.getsignal(signal.SIGINT) == original_sigint

    def test_install_idempotent(self) -> None:
        gs = GracefulShutdown()
        gs.install_signal_handlers()
        gs.install_signal_handlers()  # Should not raise
        gs.restore_signal_handlers()

    def test_restore_without_install(self) -> None:
        gs = GracefulShutdown()
        gs.restore_signal_handlers()  # Should not raise

    def test_thread_safety(self) -> None:
        gs = GracefulShutdown()
        results: list[bool] = []

        def check_and_set() -> None:
            results.append(gs.is_shutting_down)
            gs.request_shutdown()
            results.append(gs.is_shutting_down)

        t = threading.Thread(target=check_and_set)
        t.start()
        t.join()

        assert results[0] is False
        assert results[1] is True
        assert gs.is_shutting_down is True


class TestServeLoopGracefulShutdown:
    """Tests for serve_loop graceful shutdown behavior."""

    def setup_method(self) -> None:
        # Reset the module-level shutdown singleton before each test
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()
        handler._handlers_installed = False

    def teardown_method(self) -> None:
        # Clean up after each test
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()
        handler.restore_signal_handlers()

    def test_serve_loop_exits_on_shutdown(self) -> None:
        call_count = 0
        handler = get_shutdown_handler()

        def work() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                handler.request_shutdown()
            return True

        serve_loop(work, sleep_time=0.01)
        assert call_count == 3

    def test_serve_loop_skips_sleep_on_shutdown(self) -> None:
        """When shutdown is requested, the loop should not sleep even if func returns False."""
        handler = get_shutdown_handler()
        call_count = 0

        def work() -> bool:
            nonlocal call_count
            call_count += 1
            handler.request_shutdown()
            return False  # Would normally trigger sleep

        start = time.time()
        serve_loop(work, sleep_time=5.0)
        elapsed = time.time() - start

        assert call_count == 1
        assert elapsed < 2.0  # Should not have slept for 5 seconds

    def test_serve_loop_completes_current_iteration(self) -> None:
        """The current iteration's function call should complete before exit."""
        handler = get_shutdown_handler()
        completed = False

        def work() -> bool:
            nonlocal completed
            # Request shutdown during execution
            handler.request_shutdown()
            # This should still execute
            time.sleep(0.05)
            completed = True
            return True

        serve_loop(work, sleep_time=0.01)
        assert completed is True

    def test_serve_loop_validation(self) -> None:
        with pytest.raises(ValueError, match="sleep_time"):
            serve_loop(lambda: True, sleep_time=-1)
        with pytest.raises(ValueError, match="report_time"):
            serve_loop(lambda: True, report_time=-1)


class TestAsyncServeLoopGracefulShutdown:
    """Tests for async_serve_loop graceful shutdown behavior."""

    def setup_method(self) -> None:
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()
        handler._handlers_installed = False

    def teardown_method(self) -> None:
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()
        handler.restore_signal_handlers()

    def test_async_serve_loop_exits_on_shutdown(self) -> None:
        call_count = 0
        handler = get_shutdown_handler()

        async def work() -> bool:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                handler.request_shutdown()
            return True

        async def run() -> None:
            await async_serve_loop(work, sleep_time=0.01)

        asyncio.run(run())
        assert call_count == 3

    def test_async_serve_loop_skips_sleep_on_shutdown(self) -> None:
        handler = get_shutdown_handler()
        call_count = 0

        async def work() -> bool:
            nonlocal call_count
            call_count += 1
            handler.request_shutdown()
            return False

        async def run() -> None:
            await async_serve_loop(work, sleep_time=5.0)

        start = time.time()
        asyncio.run(run())
        elapsed = time.time() - start

        assert call_count == 1
        assert elapsed < 2.0

    def test_async_serve_loop_validation(self) -> None:
        async def noop() -> bool:
            return True

        with pytest.raises(ValueError, match="sleep_time"):
            asyncio.run(async_serve_loop(noop, sleep_time=-1))
        with pytest.raises(ValueError, match="report_time"):
            asyncio.run(async_serve_loop(noop, report_time=-1))


class TestModuleLevelHelpers:
    """Tests for module-level convenience functions."""

    def setup_method(self) -> None:
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()

    def teardown_method(self) -> None:
        handler = get_shutdown_handler()
        handler._shutdown_event.clear()

    def test_get_shutdown_handler_returns_singleton(self) -> None:
        h1 = get_shutdown_handler()
        h2 = get_shutdown_handler()
        assert h1 is h2

    def test_is_shutting_down_reflects_state(self) -> None:
        assert is_shutting_down() is False
        get_shutdown_handler().request_shutdown()
        assert is_shutting_down() is True
