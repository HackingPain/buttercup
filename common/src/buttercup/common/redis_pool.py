"""Shared Redis connection pooling for Buttercup CRS components.

Provides a centralized connection pool that can be shared across all Redis
clients within a process, reducing connection overhead and enabling consistent
connection management.

Configuration via environment variables:
    REDIS_HOST: Redis server hostname (default: "localhost")
    REDIS_PORT: Redis server port (default: 6379)
    REDIS_DB: Redis database number (default: 0)
    REDIS_PASSWORD: Redis password (default: None)
    REDIS_MAX_CONNECTIONS: Maximum pool size (default: 10)
    REDIS_SOCKET_TIMEOUT: Socket timeout in seconds (default: 5.0)
    REDIS_SOCKET_CONNECT_TIMEOUT: Connection timeout in seconds (default: 5.0)
    REDIS_RETRY_ON_TIMEOUT: Whether to retry on timeout (default: True)
    REDIS_HEALTH_CHECK_INTERVAL: Seconds between health checks (default: 30)
"""

from __future__ import annotations

import logging
import os
import threading

from redis import ConnectionPool, Redis, RedisError
from redis.retry import Retry

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:  # noqa: FBT001
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes")


def get_redis_connection_pool(
    host: str | None = None,
    port: int | None = None,
    db: int | None = None,
    password: str | None = None,
    max_connections: int | None = None,
    socket_timeout: float | None = None,
    socket_connect_timeout: float | None = None,
    retry_on_timeout: bool | None = None,
    health_check_interval: int | None = None,
) -> ConnectionPool:
    """Return the shared Redis connection pool, creating it on first call.

    Parameters override the corresponding environment variables. Subsequent
    calls return the same pool instance (singleton) regardless of arguments;
    to reset the pool use :func:`reset_pool`.

    Args:
        host: Redis hostname. Env: REDIS_HOST (default "localhost").
        port: Redis port. Env: REDIS_PORT (default 6379).
        db: Redis database number. Env: REDIS_DB (default 0).
        password: Redis password. Env: REDIS_PASSWORD (default None).
        max_connections: Maximum pool connections. Env: REDIS_MAX_CONNECTIONS (default 10).
        socket_timeout: Socket timeout seconds. Env: REDIS_SOCKET_TIMEOUT (default 5.0).
        socket_connect_timeout: Connect timeout seconds. Env: REDIS_SOCKET_CONNECT_TIMEOUT (default 5.0).
        retry_on_timeout: Retry on timeout. Env: REDIS_RETRY_ON_TIMEOUT (default True).
        health_check_interval: Health check interval seconds. Env: REDIS_HEALTH_CHECK_INTERVAL (default 30).

    Returns:
        A shared :class:`redis.ConnectionPool` instance.
    """
    global _pool  # noqa: PLW0603

    if _pool is not None:
        return _pool

    with _pool_lock:
        # Double-checked locking
        if _pool is not None:
            return _pool

        resolved_host = host if host is not None else _env_str("REDIS_HOST", "localhost")
        resolved_port = port if port is not None else _env_int("REDIS_PORT", 6379)
        resolved_db = db if db is not None else _env_int("REDIS_DB", 0)
        resolved_max_connections = max_connections if max_connections is not None else _env_int("REDIS_MAX_CONNECTIONS", 10)
        resolved_socket_timeout = socket_timeout if socket_timeout is not None else _env_float("REDIS_SOCKET_TIMEOUT", 5.0)
        resolved_socket_connect_timeout = (
            socket_connect_timeout if socket_connect_timeout is not None else _env_float("REDIS_SOCKET_CONNECT_TIMEOUT", 5.0)
        )
        resolved_retry_on_timeout = retry_on_timeout if retry_on_timeout is not None else _env_bool("REDIS_RETRY_ON_TIMEOUT", True)
        resolved_health_check_interval = (
            health_check_interval if health_check_interval is not None else _env_int("REDIS_HEALTH_CHECK_INTERVAL", 30)
        )

        # Resolve password: explicit arg > env var > None
        resolved_password = password if password is not None else os.getenv("REDIS_PASSWORD")

        retry = Retry(retries=3, retry_on_timeout=resolved_retry_on_timeout)

        pool = ConnectionPool(
            host=resolved_host,
            port=resolved_port,
            db=resolved_db,
            password=resolved_password,
            max_connections=resolved_max_connections,
            socket_timeout=resolved_socket_timeout,
            socket_connect_timeout=resolved_socket_connect_timeout,
            retry_on_timeout=resolved_retry_on_timeout,
            retry=retry,
            health_check_interval=resolved_health_check_interval,
        )

        logger.info(
            "Created shared Redis connection pool: host=%s, port=%d, db=%d, max_connections=%d",
            resolved_host,
            resolved_port,
            resolved_db,
            resolved_max_connections,
        )

        _pool = pool
        return _pool


def get_redis_client(
    host: str | None = None,
    port: int | None = None,
    db: int | None = None,
    password: str | None = None,
    max_connections: int | None = None,
    decode_responses: bool = False,
    **kwargs: object,
) -> Redis:
    """Return a Redis client backed by the shared connection pool.

    All keyword arguments are forwarded to :func:`get_redis_connection_pool`
    on first pool creation; they are ignored if the pool already exists.

    Args:
        host: Redis hostname override.
        port: Redis port override.
        db: Redis database override.
        password: Redis password override.
        max_connections: Maximum pool connections override.
        decode_responses: Whether to decode response bytes to strings.
        **kwargs: Additional keyword arguments forwarded to pool creation.

    Returns:
        A :class:`redis.Redis` instance using the shared pool.
    """
    pool = get_redis_connection_pool(
        host=host,
        port=port,
        db=db,
        password=password,
        max_connections=max_connections,
    )
    return Redis(connection_pool=pool, decode_responses=decode_responses)


def check_health() -> bool:
    """Ping the Redis server using the shared pool.

    Returns:
        True if the server responds to PING, False otherwise.
    """
    try:
        client = get_redis_client()
        return client.ping()
    except RedisError:
        logger.exception("Redis health check failed")
        return False


def reset_pool() -> None:
    """Disconnect and discard the shared connection pool.

    Useful for testing or when the Redis server configuration changes at
    runtime.
    """
    global _pool  # noqa: PLW0603
    with _pool_lock:
        if _pool is not None:
            _pool.disconnect()
            logger.info("Shared Redis connection pool has been reset")
            _pool = None
