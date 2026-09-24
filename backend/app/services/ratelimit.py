"""Central rate limiting + retry for outbound YouTube requests (Data API and transcripts)."""
import logging
import random
import threading
import time
from typing import Callable, TypeVar

from ..config import settings

logger = logging.getLogger("app.external")
T = TypeVar("T")

MAX_BACKOFF_SECONDS = 30.0


class RateLimiter:
    """Spaces requests evenly: at most `per_minute` requests start per minute (0 disables limiting)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] | None = None):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._clock, self._sleep = clock, sleep or time.sleep
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until this request may start; returns the seconds waited."""
        if not self.interval:
            return 0.0
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
        wait = slot - now
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)


_youtube_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_youtube_limiter() -> RateLimiter:
    global _youtube_limiter
    with _limiter_lock:
        if _youtube_limiter is None:
            _youtube_limiter = RateLimiter(settings.requests_per_minute)
        return _youtube_limiter


def reset_youtube_limiter() -> None:
    global _youtube_limiter
    with _limiter_lock:
        _youtube_limiter = None


def backoff_delay(attempt: int, retry_after: float | None = None, base: float = 1.0) -> float:
    """Exponential (1s, 2s, 4s, ...) plus a small jitter; a server Retry-After wins if larger."""
    delay = min(base * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS) + random.uniform(0, 0.25 * base)
    return min(max(delay, retry_after or 0.0), MAX_BACKOFF_SECONDS)


def call_with_retry(func: Callable[[], T], *, category: str, is_retryable: Callable[[Exception], tuple[bool, float | None]],
                    limiter: RateLimiter | None = None, max_attempts: int | None = None,
                    sleep: Callable[[float], None] | None = None, redact: Callable[[str], str] = str) -> T:
    """Run `func` under the rate limiter, retrying transient failures a bounded number of times.

    `is_retryable(exc)` returns (retry?, retry_after_seconds). Permanent errors and the
    last failed attempt re-raise the original exception.
    """
    limiter = limiter or get_youtube_limiter()
    attempts = max(1, max_attempts or settings.request_max_attempts)
    for attempt in range(1, attempts + 1):
        limiter.acquire()
        try:
            return func()
        except Exception as exc:
            retry, retry_after = is_retryable(exc)
            if not retry or attempt >= attempts:
                raise
            delay = backoff_delay(attempt, retry_after)
            logger.warning("%s failed (%s); retry %d/%d in %.1fs", category, redact(f"{type(exc).__name__}: {exc}")[:200], attempt, attempts - 1, delay)
            (sleep or time.sleep)(delay)
    raise AssertionError("unreachable")
