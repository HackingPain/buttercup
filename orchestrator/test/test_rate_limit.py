"""Tests for the rate limiting middleware."""

from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from buttercup.orchestrator.task_server.rate_limit import RateLimitStore, _TokenBucket

# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_allows_up_to_capacity(self) -> None:
        bucket = _TokenBucket(capacity=3)
        assert bucket.allow()
        assert bucket.allow()
        assert bucket.allow()
        assert not bucket.allow()

    def test_remaining_decreases(self) -> None:
        bucket = _TokenBucket(capacity=5)
        assert bucket.remaining == 5
        bucket.allow()
        assert bucket.remaining == 4

    def test_tokens_replenish_after_window(self) -> None:
        bucket = _TokenBucket(capacity=2, window_seconds=0.1)
        assert bucket.allow()
        assert bucket.allow()
        assert not bucket.allow()
        time.sleep(0.15)
        assert bucket.allow()


class TestRateLimitStore:
    def test_separate_buckets_for_general_and_heavy(self) -> None:
        store = RateLimitStore(general_limit=5, heavy_limit=2)
        # Exhaust heavy bucket
        store.check("127.0.0.1", is_heavy=True)
        store.check("127.0.0.1", is_heavy=True)
        allowed, _ = store.check("127.0.0.1", is_heavy=True)
        assert not allowed

        # General bucket should still have capacity
        allowed, _ = store.check("127.0.0.1", is_heavy=False)
        assert allowed

    def test_separate_buckets_per_ip(self) -> None:
        store = RateLimitStore(general_limit=1, heavy_limit=1)
        store.check("10.0.0.1", is_heavy=False)
        allowed_ip1, _ = store.check("10.0.0.1", is_heavy=False)
        assert not allowed_ip1

        allowed_ip2, _ = store.check("10.0.0.2", is_heavy=False)
        assert allowed_ip2

    def test_reset_clears_state(self) -> None:
        store = RateLimitStore(general_limit=1, heavy_limit=1)
        store.check("127.0.0.1", is_heavy=False)
        allowed, _ = store.check("127.0.0.1", is_heavy=False)
        assert not allowed

        store.reset()
        allowed, _ = store.check("127.0.0.1", is_heavy=False)
        assert allowed


# ---------------------------------------------------------------------------
# Integration tests with the FastAPI app
# ---------------------------------------------------------------------------

# Patch settings before importing the app (same pattern as test_server.py)
monkeypatch = pytest.MonkeyPatch()
monkeypatch.setattr("buttercup.orchestrator.task_server.config.TaskServerSettings", MagicMock)


class _TestSettings:
    api_key_id: str = "515cc8a0-3019-4c9f-8c1c-72d0b54ae561"
    api_token: str = "VGuAC8axfOnFXKBB7irpNDOKcDjOlnyB"
    api_token_hash: str = (
        "$argon2id$v=19$m=65536,t=3,p=4$Dg1v6NPGTyXPoOPF4ozD5A$wa/85ttk17bBsIASSwdR/uGz5UKN/bZuu4wu+JIy1iA"
    )
    log_level: str = "debug"
    log_max_line_length: int | None = None
    redis_url: str = "redis://localhost:6379"
    competition_api_url: str = "http://localhost:31323"
    competition_api_username: str = "11111111-1111-1111-1111-111111111111"
    competition_api_password: str = "secret"
    rate_limit_enabled: bool = True
    rate_limit_general: int = 60
    rate_limit_heavy: int = 10


settings = _TestSettings()
monkeypatch.setattr("buttercup.orchestrator.task_server.dependencies.get_settings", lambda: settings)

from buttercup.orchestrator.task_server.dependencies import get_delete_task_queue, get_task_queue  # noqa: E402
from buttercup.orchestrator.task_server.server import app, rate_limit_store  # noqa: E402

mock_tasks_queue = MagicMock()
mock_delete_task_queue = MagicMock()
app.dependency_overrides[get_task_queue] = lambda: mock_tasks_queue
app.dependency_overrides[get_delete_task_queue] = lambda: mock_delete_task_queue


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Generator[None, None, None]:
    """Reset rate limit state before each test."""
    rate_limit_store.reset()
    yield
    rate_limit_store.reset()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


class TestRateLimitMiddlewareIntegration:
    """Test rate limiting through the actual FastAPI application."""

    def test_rate_limit_headers_present(self, client: TestClient) -> None:
        """Successful responses include rate limit headers."""
        response = client.get("/api/version")
        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    def test_general_endpoint_rate_limited(self, client: TestClient) -> None:
        """General endpoints return 429 after exhausting the budget."""
        # Override store to a very small limit for testing
        rate_limit_store.general_limit = 3
        rate_limit_store.reset()
        rate_limit_store._general.default_factory = lambda: _TokenBucket(capacity=3)  # type: ignore[assignment]

        for _ in range(3):
            resp = client.get("/api/version")
            assert resp.status_code == 200

        resp = client.get("/api/version")
        assert resp.status_code == 429
        body = resp.json()
        assert "Rate limit exceeded" in body["detail"]
        assert "Retry-After" in resp.headers

    @patch("buttercup.orchestrator.task_server.server.create_api_client")
    def test_heavy_endpoint_rate_limited(self, mock_create_api_client: MagicMock, client: TestClient) -> None:
        """Heavy (write) endpoints use the stricter limit."""
        rate_limit_store.heavy_limit = 2
        rate_limit_store.reset()
        rate_limit_store._heavy.default_factory = lambda: _TokenBucket(capacity=2)  # type: ignore[assignment]

        auth = (settings.api_key_id, settings.api_token)
        task_id = uuid4()

        for _ in range(2):
            resp = client.delete(f"/api/v1/task/{task_id}/", auth=auth)
            assert resp.status_code == 200

        resp = client.delete(f"/api/v1/task/{task_id}/", auth=auth)
        assert resp.status_code == 429

    @patch("buttercup.orchestrator.task_server.server.create_api_client")
    def test_heavy_limit_does_not_affect_general(
        self, mock_create_api_client: MagicMock, client: TestClient
    ) -> None:
        """Exhausting the heavy budget does not affect general endpoints."""
        rate_limit_store.heavy_limit = 1
        rate_limit_store.reset()
        rate_limit_store._heavy.default_factory = lambda: _TokenBucket(capacity=1)  # type: ignore[assignment]

        auth = (settings.api_key_id, settings.api_token)

        # Exhaust heavy budget
        client.delete(f"/api/v1/task/{uuid4()}/", auth=auth)
        resp = client.delete(f"/api/v1/task/{uuid4()}/", auth=auth)
        assert resp.status_code == 429

        # General endpoint should still work
        resp = client.get("/api/version")
        assert resp.status_code == 200

    def test_rate_limit_per_ip(self, client: TestClient) -> None:
        """Different IPs have independent rate limit budgets."""
        # Since TestClient always uses the same IP (testclient), we test
        # via the store directly.
        rate_limit_store.reset()
        rate_limit_store.general_limit = 1
        rate_limit_store._general.default_factory = lambda: _TokenBucket(capacity=1)  # type: ignore[assignment]

        allowed1, _ = rate_limit_store.check("1.2.3.4", is_heavy=False)
        assert allowed1
        allowed2, _ = rate_limit_store.check("1.2.3.4", is_heavy=False)
        assert not allowed2

        # Different IP still has budget
        allowed3, _ = rate_limit_store.check("5.6.7.8", is_heavy=False)
        assert allowed3
