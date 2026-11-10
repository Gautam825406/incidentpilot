"""In-memory sliding-window rate limiting (Phase 4).

No new infrastructure dependency: a per-key deque of request timestamps,
pruned to the window on every check. This is demo-grade protection against
credential stuffing on /api/auth/login and alert floods on
/api/webhooks/alert — it does NOT survive a restart and does NOT share
state across processes/workers. Behind a multi-worker deployment, rate
limiting belongs in the reverse proxy (or Redis); see README.
"""
import time
from collections import deque


class RateLimiter:
    """Allow at most ``limit`` calls per ``key`` per ``window_seconds``."""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        """True if this call is within the limit (and records it)."""
        now = time.time() if now is None else now
        window = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        return True

    def reset(self) -> None:
        """Drop all recorded hits (used by the test suite between tests)."""
        self._hits.clear()
