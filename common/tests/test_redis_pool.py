"""Tests for the shared Redis connection pool module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from redis import ConnectionPool, Redis, RedisError

from buttercup.common import redis_pool
from buttercup.common.redis_pool import (
    check_health,
    get_redis_client,
    get_redis_connection_pool,
    reset_pool,
)


@pytest.fixture(autouse=True)
def _reset_pool_between_tests():
    """Ensure the global pool is reset before and after every test."""
    redis_pool._pool = None
    yield
    redis_pool._pool = None


class TestGetRedisConnectionPool:
    def test_returns_connection_pool_instance(self):
        pool = get_redis_connection_pool()
        assert isinstance(pool, ConnectionPool)

    def test_returns_same_instance_on_repeated_calls(self):
        pool1 = get_redis_connection_pool()
        pool2 = get_redis_connection_pool()
        assert pool1 is pool2

    def test_default_max_connections(self):
        pool = get_redis_connection_pool()
        assert pool.max_connections == 10

    def test_custom_max_connections_via_argument(self):
        pool = get_redis_connection_pool(max_connections=25)
        assert pool.max_connections == 25

    def test_custom_max_connections_via_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "42")
        pool = get_redis_connection_pool()
        assert pool.max_connections == 42

    def test_argument_overrides_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "42")
        pool = get_redis_connection_pool(max_connections=7)
        assert pool.max_connections == 7

    def test_default_host_and_port(self):
        pool = get_redis_connection_pool()
        kwargs = pool.connection_kwargs
        assert kwargs["host"] == "localhost"
        assert kwargs["port"] == 6379

    def test_custom_host_and_port(self):
        pool = get_redis_connection_pool(host="redis.example.com", port=6380)
        kwargs = pool.connection_kwargs
        assert kwargs["host"] == "redis.example.com"
        assert kwargs["port"] == 6380

    def test_custom_host_via_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REDIS_HOST", "myhost")
        monkeypatch.setenv("REDIS_PORT", "7777")
        pool = get_redis_connection_pool()
        kwargs = pool.connection_kwargs
        assert kwargs["host"] == "myhost"
        assert kwargs["port"] == 7777

    def test_default_db(self):
        pool = get_redis_connection_pool()
        assert pool.connection_kwargs["db"] == 0

    def test_custom_db(self):
        pool = get_redis_connection_pool(db=5)
        assert pool.connection_kwargs["db"] == 5

    def test_password_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REDIS_PASSWORD", "secret123")
        pool = get_redis_connection_pool()
        assert pool.connection_kwargs["password"] == "secret123"

    def test_password_explicit(self):
        pool = get_redis_connection_pool(password="explicit")
        assert pool.connection_kwargs["password"] == "explicit"

    def test_socket_timeout_defaults(self):
        pool = get_redis_connection_pool()
        kwargs = pool.connection_kwargs
        assert kwargs["socket_timeout"] == 5.0
        assert kwargs["socket_connect_timeout"] == 5.0

    def test_socket_timeout_custom(self):
        pool = get_redis_connection_pool(socket_timeout=10.0, socket_connect_timeout=3.0)
        kwargs = pool.connection_kwargs
        assert kwargs["socket_timeout"] == 10.0
        assert kwargs["socket_connect_timeout"] == 3.0

    def test_retry_on_timeout_default(self):
        pool = get_redis_connection_pool()
        assert pool.connection_kwargs["retry_on_timeout"] is True

    def test_retry_on_timeout_disabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("REDIS_RETRY_ON_TIMEOUT", "false")
        pool = get_redis_connection_pool()
        assert pool.connection_kwargs["retry_on_timeout"] is False

    def test_health_check_interval_default(self):
        pool = get_redis_connection_pool()
        assert pool.connection_kwargs["health_check_interval"] == 30

    def test_health_check_interval_custom(self):
        pool = get_redis_connection_pool(health_check_interval=60)
        assert pool.connection_kwargs["health_check_interval"] == 60

    def test_singleton_ignores_later_args(self):
        """Once the pool is created, subsequent calls return the same pool."""
        pool1 = get_redis_connection_pool(max_connections=5)
        pool2 = get_redis_connection_pool(max_connections=99)
        assert pool1 is pool2
        assert pool1.max_connections == 5


class TestGetRedisClient:
    def test_returns_redis_instance(self):
        client = get_redis_client()
        assert isinstance(client, Redis)

    def test_client_uses_shared_pool(self):
        client = get_redis_client()
        pool = get_redis_connection_pool()
        assert client.connection_pool is pool

    def test_decode_responses_false_by_default(self):
        client = get_redis_client()
        assert client.connection_pool.connection_kwargs.get("decode_responses", False) is False

    def test_multiple_clients_share_pool(self):
        c1 = get_redis_client()
        c2 = get_redis_client()
        assert c1.connection_pool is c2.connection_pool


class TestCheckHealth:
    @patch("buttercup.common.redis_pool.get_redis_client")
    def test_healthy(self, mock_get_client: MagicMock):
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_get_client.return_value = mock_client
        assert check_health() is True

    @patch("buttercup.common.redis_pool.get_redis_client")
    def test_unhealthy(self, mock_get_client: MagicMock):
        mock_client = MagicMock()
        mock_client.ping.side_effect = RedisError("Connection refused")
        mock_get_client.return_value = mock_client
        assert check_health() is False


class TestResetPool:
    def test_reset_clears_pool(self):
        _ = get_redis_connection_pool()
        assert redis_pool._pool is not None
        reset_pool()
        assert redis_pool._pool is None

    def test_reset_allows_new_pool_creation(self):
        pool1 = get_redis_connection_pool(max_connections=5)
        reset_pool()
        pool2 = get_redis_connection_pool(max_connections=20)
        assert pool1 is not pool2
        assert pool2.max_connections == 20

    def test_reset_when_no_pool_exists(self):
        """Calling reset when no pool exists should not raise."""
        reset_pool()
        assert redis_pool._pool is None


class TestThreadSafety:
    def test_concurrent_pool_creation(self):
        """Multiple threads requesting the pool should all get the same instance."""
        import concurrent.futures

        results: list[ConnectionPool] = []

        def get_pool():
            return get_redis_connection_pool()

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(get_pool) for _ in range(20)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        # All threads must have received the same pool instance
        assert all(r is results[0] for r in results)
