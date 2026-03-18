"""Tests for the retry_llm decorator."""

import asyncio
from unittest.mock import MagicMock, patch

import openai
import pytest

from buttercup.common.llm import retry_llm


class TestRetryLlmSync:
    """Tests for synchronous function retry."""

    def test_succeeds_first_try(self) -> None:
        mock_fn = MagicMock(return_value="ok")

        @retry_llm
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 1

    def test_retries_on_rate_limit(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                "ok",
            ]
        )

        @retry_llm(max_retries=3, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 2

    def test_retries_on_internal_server_error(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.InternalServerError(
                    message="server error",
                    response=MagicMock(status_code=500, headers={}),
                    body=None,
                ),
                "ok",
            ]
        )

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 2

    def test_retries_on_connection_error(self) -> None:
        mock_fn = MagicMock(side_effect=[ConnectionError("reset"), "ok"])

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 2

    def test_retries_on_timeout_error(self) -> None:
        mock_fn = MagicMock(side_effect=[TimeoutError("timed out"), "ok"])

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 2

    def test_retries_on_api_timeout_error(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.APITimeoutError(request=MagicMock()),
                "ok",
            ]
        )

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        assert call_llm() == "ok"
        assert mock_fn.call_count == 2

    def test_does_not_retry_on_bad_request(self) -> None:
        mock_fn = MagicMock(
            side_effect=openai.BadRequestError(
                message="bad request",
                response=MagicMock(status_code=400, headers={}),
                body=None,
            )
        )

        @retry_llm(max_retries=3, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        with pytest.raises(openai.BadRequestError):
            call_llm()
        assert mock_fn.call_count == 1

    def test_does_not_retry_on_auth_error(self) -> None:
        mock_fn = MagicMock(
            side_effect=openai.AuthenticationError(
                message="invalid key",
                response=MagicMock(status_code=401, headers={}),
                body=None,
            )
        )

        @retry_llm(max_retries=3, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        with pytest.raises(openai.AuthenticationError):
            call_llm()
        assert mock_fn.call_count == 1

    def test_raises_after_max_retries(self) -> None:
        mock_fn = MagicMock(
            side_effect=openai.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        with pytest.raises(openai.RateLimitError):
            call_llm()
        # 1 initial + 2 retries = 3
        assert mock_fn.call_count == 3

    def test_exponential_backoff_delays(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                openai.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                "ok",
            ]
        )
        sleep_calls: list[float] = []

        @retry_llm(max_retries=3, base_delay=1.0, max_delay=60.0)
        def call_llm() -> str:
            return mock_fn()

        with patch("buttercup.common.llm.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            assert call_llm() == "ok"

        assert len(sleep_calls) == 2
        # base_delay * 2^0 = 1.0, base_delay * 2^1 = 2.0
        assert sleep_calls[0] == pytest.approx(1.0)
        assert sleep_calls[1] == pytest.approx(2.0)

    def test_max_delay_cap(self) -> None:
        errors = [
            openai.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
            for _ in range(4)
        ]
        mock_fn = MagicMock(side_effect=[*errors, "ok"])
        sleep_calls: list[float] = []

        @retry_llm(max_retries=5, base_delay=1.0, max_delay=5.0)
        def call_llm() -> str:
            return mock_fn()

        with patch("buttercup.common.llm.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            assert call_llm() == "ok"

        # delays: 1, 2, 4, 5 (capped at max_delay)
        assert sleep_calls == [pytest.approx(1.0), pytest.approx(2.0), pytest.approx(4.0), pytest.approx(5.0)]

    def test_bare_decorator_no_parens(self) -> None:
        """Test that @retry_llm without parentheses works."""
        mock_fn = MagicMock(side_effect=[ConnectionError("fail"), "ok"])

        @retry_llm
        def call_llm() -> str:
            return mock_fn()

        with patch("buttercup.common.llm.time.sleep"):
            assert call_llm() == "ok"

    def test_logs_retry_attempts(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                "ok",
            ]
        )

        @retry_llm(max_retries=2, base_delay=0.01)
        def call_llm() -> str:
            return mock_fn()

        with patch("buttercup.common.llm.logger") as mock_logger:
            call_llm()
            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args[0]
            assert "attempt 1/2" in call_args[0] % call_args[1:]


class TestRetryLlmAsync:
    """Tests for async function retry."""

    def test_async_succeeds_first_try(self) -> None:
        mock_fn = MagicMock(return_value="ok")

        @retry_llm(max_retries=2, base_delay=0.01)
        async def call_llm() -> str:
            return mock_fn()

        result = asyncio.get_event_loop().run_until_complete(call_llm())
        assert result == "ok"
        assert mock_fn.call_count == 1

    def test_async_retries_on_rate_limit(self) -> None:
        mock_fn = MagicMock(
            side_effect=[
                openai.RateLimitError(
                    message="rate limit",
                    response=MagicMock(status_code=429, headers={}),
                    body=None,
                ),
                "ok",
            ]
        )

        @retry_llm(max_retries=2, base_delay=0.01)
        async def call_llm() -> str:
            return mock_fn()

        result = asyncio.get_event_loop().run_until_complete(call_llm())
        assert result == "ok"
        assert mock_fn.call_count == 2

    def test_async_raises_after_max_retries(self) -> None:
        mock_fn = MagicMock(
            side_effect=openai.RateLimitError(
                message="rate limit",
                response=MagicMock(status_code=429, headers={}),
                body=None,
            )
        )

        @retry_llm(max_retries=1, base_delay=0.01)
        async def call_llm() -> str:
            return mock_fn()

        with pytest.raises(openai.RateLimitError):
            asyncio.get_event_loop().run_until_complete(call_llm())
        assert mock_fn.call_count == 2

    def test_async_does_not_retry_non_transient(self) -> None:
        mock_fn = MagicMock(side_effect=ValueError("bad value"))

        @retry_llm(max_retries=3, base_delay=0.01)
        async def call_llm() -> str:
            return mock_fn()

        with pytest.raises(ValueError):
            asyncio.get_event_loop().run_until_complete(call_llm())
        assert mock_fn.call_count == 1
