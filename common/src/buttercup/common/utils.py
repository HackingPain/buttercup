import asyncio
import errno
import logging
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Coroutine
from os import PathLike
from pathlib import Path
from types import FrameType
from typing import Any

logger = logging.getLogger(__name__)

# Global variables for periodic reaper
_reaper_thread = None
_reaper_stop_event = None


class GracefulShutdown:
    """Manages graceful shutdown state for long-running service loops.

    Installs signal handlers for SIGTERM and SIGINT that set a shutdown flag.
    The serve_loop and async_serve_loop functions check this flag between iterations
    to exit cleanly, allowing in-progress work to complete before stopping.

    Usage as a context manager::

        with GracefulShutdown() as gs:
            while not gs.is_shutting_down:
                do_work()

    Or use the module-level singleton via request_shutdown / is_shutting_down helpers.
    """

    def __init__(self) -> None:
        self._shutdown_event = threading.Event()
        self._original_sigterm: Any = None
        self._original_sigint: Any = None
        self._handlers_installed = False

    @property
    def is_shutting_down(self) -> bool:
        """Return True if a shutdown signal has been received."""
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        """Programmatically request a graceful shutdown."""
        if not self._shutdown_event.is_set():
            logger.info("Graceful shutdown requested")
            self._shutdown_event.set()

    def _signal_handler(self, signum: int, frame: FrameType | None) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s signal, initiating graceful shutdown", sig_name)
        self._shutdown_event.set()

    def install_signal_handlers(self) -> None:
        """Install SIGTERM and SIGINT handlers. Safe to call multiple times."""
        if self._handlers_installed:
            return
        try:
            self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
            self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)
            self._handlers_installed = True
            logger.debug("Graceful shutdown signal handlers installed")
        except (OSError, ValueError):
            # signal.signal can only be called from the main thread; if we're not
            # on the main thread, skip handler installation silently.
            logger.debug("Could not install signal handlers (not on main thread)")

    def restore_signal_handlers(self) -> None:
        """Restore original signal handlers."""
        if not self._handlers_installed:
            return
        try:
            if self._original_sigterm is not None:
                signal.signal(signal.SIGTERM, self._original_sigterm)
            if self._original_sigint is not None:
                signal.signal(signal.SIGINT, self._original_sigint)
            self._handlers_installed = False
            logger.debug("Original signal handlers restored")
        except (OSError, ValueError):
            pass

    def __enter__(self) -> "GracefulShutdown":
        self.install_signal_handlers()
        return self

    def __exit__(self, *args: Any) -> None:
        self.restore_signal_handlers()


# Module-level singleton used by serve_loop / async_serve_loop.
_shutdown = GracefulShutdown()


def get_shutdown_handler() -> GracefulShutdown:
    """Return the module-level GracefulShutdown singleton.

    Services can use this to check shutdown state or register additional
    cleanup logic::

        shutdown = get_shutdown_handler()
        if shutdown.is_shutting_down:
            # finish up
    """
    return _shutdown


def is_shutting_down() -> bool:
    """Convenience check for whether a graceful shutdown has been requested."""
    return _shutdown.is_shutting_down


def copyanything(src: PathLike, dst: PathLike, **kwargs: Any) -> None:
    """Copy a file or directory to a destination.
    This function will:
    - Copy directories recursively
    - Copy single files
    - Handle existing destinations
    """
    src, dst = Path(src), Path(dst)
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore_dangling_symlinks=True, **kwargs)
    except shutil.Error:
        logger.exception(f"Some errors occurred while copying {src} to {dst}, continuing anyway...")
    except OSError as exc:  # python >2.5
        if exc.errno in (errno.ENOTDIR, errno.EINVAL):
            shutil.copy(src, dst)
        else:
            raise


def get_diffs(path: Path | None) -> list[Path]:
    """Get all diff files in the given path."""
    if path is None:
        return []
    logger.info(f"Getting diffs from {path}")
    diff_files = list(path.rglob("*.patch")) + list(path.rglob("*.diff"))
    if not diff_files:
        # If no .patch or .diff files found, try any file
        diff_files = list(path.rglob("*"))

    return sorted(diff_files)


def signal_alive_health_check() -> None:
    """Signal that the process is alive by writing the current time to a temporary file."""
    tmp_file = "/tmp/health_check_alive.tmp"
    with open(tmp_file, "w") as f:
        f.write(str(int(time.time())))
    shutil.move(tmp_file, "/tmp/health_check_alive")


def serve_loop(func: Callable[[], bool], sleep_time: float = 1.0, report_time: float = 60.0) -> None:
    """Serve a function in a loop.

    Installs signal handlers for SIGTERM/SIGINT so the loop exits cleanly
    after the current iteration completes. The called function will finish
    its work before the loop terminates.
    """
    if sleep_time < 0:
        raise ValueError("sleep_time must be greater than 0")

    if report_time < 0:
        raise ValueError("report_time must be greater than 0")

    _shutdown.install_signal_handlers()

    did_work = False
    start_time = time.time()

    while not _shutdown.is_shutting_down:
        signal_alive_health_check()
        if time.time() - start_time > report_time:
            logger.info("Sleeping, waiting for inputs")
            start_time = time.time()

        did_work = func()
        if not did_work and not _shutdown.is_shutting_down:
            time.sleep(sleep_time)

    logger.info("Serve loop exiting due to graceful shutdown")


async def async_serve_loop(
    func: Callable[[], Coroutine[Any, Any, bool]], sleep_time: float = 1.0, report_time: float = 60.0
) -> None:
    """Serve an async function in a loop.

    Installs signal handlers for SIGTERM/SIGINT so the loop exits cleanly
    after the current iteration completes. The called function will finish
    its work before the loop terminates.
    """
    if sleep_time < 0:
        raise ValueError("sleep_time must be greater than 0")

    if report_time < 0:
        raise ValueError("report_time must be greater than 0")

    _shutdown.install_signal_handlers()

    did_work = False
    start_time = time.time()

    while not _shutdown.is_shutting_down:
        signal_alive_health_check()
        if time.time() - start_time > report_time:
            logger.info("Sleeping, waiting for inputs")
            start_time = time.time()

        did_work = await func()
        if not did_work and not _shutdown.is_shutting_down:
            await asyncio.sleep(sleep_time)

    logger.info("Async serve loop exiting due to graceful shutdown")


def setup_periodic_zombie_reaper(interval_seconds: int = 5) -> None:
    """Set up a background thread that periodically reaps zombie processes."""

    def periodic_reaper() -> None:
        """Background thread function that periodically reaps zombies."""
        logger.info(f"Started periodic zombie reaper (interval: {interval_seconds}s)")

        while not _shutdown.is_shutting_down:
            time.sleep(interval_seconds)
            reaped_count = 0
            try:
                # Reap all available zombie processes
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                        if pid == 0:
                            break  # No more zombie processes
                        reaped_count += 1
                        logger.debug(f"Periodic reaper: reaped zombie PID {pid}")
                    except OSError:
                        # No more child processes to reap
                        break

                if reaped_count > 0:
                    logger.info(f"Periodic reaper: cleaned up {reaped_count} zombie processes")

            except OSError as e:
                logger.error(f"Error in periodic zombie reaper: {e}")

    # Start the daemon thread and forget about it
    thread = threading.Thread(target=periodic_reaper, daemon=True, name="ZombieReaper")
    thread.start()
    logger.info("Periodic zombie reaper started")
