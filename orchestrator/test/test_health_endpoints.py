"""Tests for health check endpoints on the task server and UI competition API."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Task Server health endpoints
# ---------------------------------------------------------------------------

# Patch settings before importing the app (same pattern as test_server.py)
monkeypatch = pytest.MonkeyPatch()
monkeypatch.setattr("buttercup.orchestrator.task_server.config.TaskServerSettings", MagicMock)


class _TaskServerSettings:
    api_key_id: str = "515cc8a0-3019-4c9f-8c1c-72d0b54ae561"
    api_token_hash: str = (
        "$argon2id$v=19$m=65536,t=3,p=4$Dg1v6NPGTyXPoOPF4ozD5A$wa/85ttk17bBsIASSwdR/uGz5UKN/bZuu4wu+JIy1iA"
    )
    log_level: str = "debug"
    log_max_line_length: int | None = None
    redis_url: str = "redis://localhost:6379"
    competition_api_url: str = "http://localhost:31323"
    competition_api_username: str = "11111111-1111-1111-1111-111111111111"
    competition_api_password: str = "secret"
    rate_limit_enabled: bool = False
    rate_limit_general: int = 100
    rate_limit_heavy: int = 10


_ts_settings = _TaskServerSettings()
monkeypatch.setattr("buttercup.orchestrator.task_server.dependencies.get_settings", lambda: _ts_settings)

from buttercup.orchestrator.task_server.dependencies import get_delete_task_queue, get_redis, get_task_queue  # noqa: E402
from buttercup.orchestrator.task_server.server import app as task_server_app  # noqa: E402

mock_tasks_queue = MagicMock()
mock_delete_task_queue = MagicMock()
task_server_app.dependency_overrides[get_task_queue] = lambda: mock_tasks_queue
task_server_app.dependency_overrides[get_delete_task_queue] = lambda: mock_delete_task_queue


@pytest.fixture
def ts_client() -> Generator[TestClient, None, None]:
    with TestClient(task_server_app) as c:
        yield c


def test_task_server_healthz(ts_client: TestClient) -> None:
    """GET /healthz should return 200 without authentication."""
    response = ts_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("buttercup.orchestrator.task_server.server.get_redis")
def test_task_server_readyz_ok(mock_get_redis: MagicMock, ts_client: TestClient) -> None:
    """GET /readyz should return 200 when Redis is reachable."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_get_redis.return_value = mock_redis

    response = ts_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_redis.ping.assert_called_once()


@patch("buttercup.orchestrator.task_server.server.get_redis")
def test_task_server_readyz_redis_down(mock_get_redis: MagicMock, ts_client: TestClient) -> None:
    """GET /readyz should return 503 when Redis is unreachable."""
    mock_get_redis.side_effect = ConnectionError("Redis unavailable")

    response = ts_client.get("/readyz")
    assert response.status_code == 503
    assert "Redis" in response.json()["detail"]


# ---------------------------------------------------------------------------
# UI Competition API health endpoints
# ---------------------------------------------------------------------------

# Patch the UI Settings class before importing the app to avoid cli_parse_args conflicts
monkeypatch.setattr("buttercup.orchestrator.ui.config.Settings", MagicMock)

from buttercup.orchestrator.ui.competition_api.main import app as ui_app  # noqa: E402
from buttercup.orchestrator.ui.competition_api.main import get_database_manager  # noqa: E402


@pytest.fixture
def ui_client() -> Generator[TestClient, None, None]:
    with TestClient(ui_app) as c:
        yield c


def test_ui_healthz(ui_client: TestClient) -> None:
    """GET /healthz should return 200 without authentication."""
    response = ui_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("buttercup.orchestrator.ui.competition_api.main.get_database_manager")
def test_ui_readyz_ok(mock_get_db: MagicMock, ui_client: TestClient) -> None:
    """GET /readyz should return 200 when the database is reachable."""
    mock_db = MagicMock()
    mock_db.get_all_tasks.return_value = []
    mock_get_db.return_value = mock_db

    response = ui_client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@patch("buttercup.orchestrator.ui.competition_api.main.get_database_manager")
def test_ui_readyz_db_down(mock_get_db: MagicMock, ui_client: TestClient) -> None:
    """GET /readyz should return 503 when the database is unreachable."""
    mock_get_db.side_effect = Exception("DB unavailable")

    response = ui_client.get("/readyz")
    assert response.status_code == 503
    assert "Database" in response.json()["detail"]
