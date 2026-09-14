"""In-process TTL cache for dashboard R2 reads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Any, Callable


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self._lock = Lock()
        self._ready = Condition(self._lock)
        self._loading: set[str] = set()
        self.hits = 0
        self.misses = 0
        self.sets = 0

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            ent = self._data.get(key)
            if ent is None or ent.expires_at <= now:
                return None
            self.hits += 1
            return ent.value

    def set(self, key: str, value: Any, ttl_sec: float) -> None:
        with self._lock:
            self._data[key] = _Entry(value, time.monotonic() + ttl_sec)
            self.sets += 1

    def get_or_set(self, key: str, ttl_sec: float, factory: Callable[[], Any]) -> tuple[Any, bool]:
        with self._ready:
            while key in self._loading:
                self._ready.wait()
                ent = self._data.get(key)
                if (
                    ent is not None
                    and ent.value is not None
                    and ent.expires_at > time.monotonic()
                ):
                    self.hits += 1
                    return ent.value, True
            ent = self._data.get(key)
            if (
                ent is not None
                and ent.value is not None
                and ent.expires_at > time.monotonic()
            ):
                self.hits += 1
                return ent.value, True
            self._loading.add(key)

        try:
            value = factory()
        except BaseException:
            with self._ready:
                self._loading.remove(key)
                self._ready.notify_all()
            raise

        with self._ready:
            self._data[key] = _Entry(value, time.monotonic() + ttl_sec)
            self.sets += 1
            self.misses += 1
            self._loading.remove(key)
            self._ready.notify_all()
        return value, False

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            live = sum(1 for e in self._data.values() if e.expires_at > now)
            return {
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "entries_live": live,
                "entries_total": len(self._data),
            }


dashboard_cache = TTLCache()
