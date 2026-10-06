"""Small in-process rate limiter for the pilot's single-worker deployment."""
from __future__ import annotations

import math
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic


class RateLimiter:
    """Bounded, thread-safe sliding-window events keyed by client address."""

    def __init__(self, *, limit: int, window_seconds: int, max_clients: int = 10_000):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def retry_after(self, key: str) -> int | None:
        """Return the wait for an already-full window without consuming a slot."""
        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests.get(key)
            if requests is None:
                return None

            while requests and requests[0] <= cutoff:
                requests.popleft()
            if not requests:
                self._requests.pop(key, None)
                return None
            self._requests.move_to_end(key)
            return self._retry_after(requests, now)

    def record(self, key: str) -> int | None:
        """Record one event, or return the wait when its window is already full."""
        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests.get(key)
            if requests is None:
                if len(self._requests) >= self.max_clients:
                    self._requests.popitem(last=False)
                requests = deque()
                self._requests[key] = requests
            else:
                self._requests.move_to_end(key)

            while requests and requests[0] <= cutoff:
                requests.popleft()
            retry_after = self._retry_after(requests, now)
            if retry_after is not None:
                return retry_after
            requests.append(now)
            return None

    def _retry_after(self, requests: deque[float], now: float) -> int | None:
        if len(requests) < self.limit:
            return None
        return max(1, math.ceil(requests[0] + self.window_seconds - now))

    def clear(self) -> None:
        with self._lock:
            self._requests.clear()
