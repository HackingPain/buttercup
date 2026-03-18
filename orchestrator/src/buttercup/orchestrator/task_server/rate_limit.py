"""Lightweight in-memory rate limiting middleware for the task server.

Rate limits are applied per client IP address using a sliding-window counter
stored in an in-memory dictionary.  The middleware distinguishes between
*general* endpoints and *heavy* endpoints (task creation / deletion) so that
write-heavy operations can be throttled more aggressively.

Configuration is driven by :class:`~buttercup.orchestrator.task_server.config.TaskServerSettings`:

* ``BUTTERCUP_TASK_SERVER_RATE_LIMIT_ENABLED`` – toggle (default ``True``)
* ``BUTTERCUP_TASK_SERVER_RATE_LIMIT_GENERAL`` – max requests/min for reads (default ``60``)
* ``BUTTERCUP_TASK_SERVER_RATE_LIMIT_HEAVY``   – max requests/min for writes (default ``10``)
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

# Paths (and HTTP methods) that are considered "heavy" operations.
# These receive the stricter ``rate_limit_heavy`` budget.
_HEAVY_OPERATIONS: set[tuple[str, str]] = {
    ("POST", "/api/v1/task/"),
    ("POST", "/v1/task/"),
    ("DELETE", "/api/v1/task/"),
    ("DELETE", "/v1/task/"),
    ("POST", "/api/v1/sarif/"),
    ("POST", "/v1/sarif/"),
}


def _is_heavy(method: str, path: str) -> bool:
    """Return ``True`` when the request targets a heavy endpoint.

    We also match DELETE on ``/api/v1/task/{uuid}/`` and ``/v1/task/{uuid}/``
    by checking the prefix + method rather than enumerating every possible UUID.
    """
    if (method, path) in _HEAVY_OPERATIONS:
        return True
    # DELETE /api/v1/task/{task_id}/ or /v1/task/{task_id}/
    if method == "DELETE" and (path.startswith("/api/v1/task/") or path.startswith("/v1/task/")):
        return True
    return False


@dataclass
class _TokenBucket:
    """Simple sliding-window rate limiter for a single client."""

    capacity: int
    window_seconds: float = 60.0
    _timestamps: list[float] = field(default_factory=list)

    def allow(self) -> bool:
        """Return ``True`` if the request is allowed, consuming one token."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Evict expired entries
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self.capacity:
            return False
        self._timestamps.append(now)
        return True

    @property
    def remaining(self) -> int:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        return max(0, self.capacity - len(self._timestamps))


class RateLimitStore:
    """Thread-safe (GIL-protected) per-IP rate-limit store.

    Maintains separate buckets for *general* and *heavy* request categories.
    """

    def __init__(self, general_limit: int, heavy_limit: int) -> None:
        self.general_limit = general_limit
        self.heavy_limit = heavy_limit
        self._general: dict[str, _TokenBucket] = defaultdict(lambda: _TokenBucket(capacity=general_limit))
        self._heavy: dict[str, _TokenBucket] = defaultdict(lambda: _TokenBucket(capacity=heavy_limit))

    def check(self, client_ip: str, is_heavy: bool) -> tuple[bool, int]:
        """Check and consume a token.  Returns ``(allowed, remaining)``."""
        bucket = self._heavy[client_ip] if is_heavy else self._general[client_ip]
        allowed = bucket.allow()
        return allowed, bucket.remaining

    def reset(self) -> None:
        """Clear all stored buckets (useful for testing)."""
        self._general.clear()
        self._heavy.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces per-IP rate limits."""

    def __init__(self, app: FastAPI, *, store: RateLimitStore, enabled: bool = True) -> None:
        super().__init__(app)
        self.store = store
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        heavy = _is_heavy(request.method, request.url.path)
        allowed, remaining = self.store.check(client_ip, heavy)

        if not allowed:
            limit = self.store.heavy_limit if heavy else self.store.general_limit
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        limit = self.store.heavy_limit if heavy else self.store.general_limit
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
