from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic


@dataclass
class _Window:
    started_at: float
    count: int


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int = 60,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._limit = max(limit, 1)
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, _Window] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window.started_at >= self._window_seconds:
            self._windows[key] = _Window(started_at=now, count=1)
            return True
        if window.count >= self._limit:
            return False
        window.count += 1
        return True
