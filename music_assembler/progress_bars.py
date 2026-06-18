"""A tiny thread-safe multi-line progress display for parallel jobs.

Each job owns one line: ``label |#####-----|  52.3%  message``. Workers call
``update(index, pct, msg)`` from any thread; a lock serializes repaints so the block
of lines stays consistent. Uses ANSI cursor-up to redraw in place, with a plain
fallback (no in-place redraw) when stderr isn't a TTY.
"""

from __future__ import annotations

import sys
import threading


class MultiProgress:
    def __init__(self, labels: list[str], *, width: int = 28, stream=None) -> None:
        self.labels = list(labels)
        self.n = len(self.labels)
        self.width = width
        self.stream = stream or sys.stderr
        self.pcts = [0.0] * self.n
        self.msgs = ["queued"] * self.n
        self._lock = threading.Lock()
        self._started = False
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._label_w = max((len(s) for s in self.labels), default=0)

    def __enter__(self) -> "MultiProgress":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            if self._tty:
                for i in range(self.n):
                    self.stream.write(self._line(i) + "\n")
                self.stream.flush()

    def update(self, index: int, pct: float, msg: str) -> None:
        with self._lock:
            if not (0 <= index < self.n):
                return
            self.pcts[index] = max(0.0, min(100.0, pct))
            self.msgs[index] = msg
            if self._tty:
                self._repaint_locked()
            else:
                # Non-TTY (e.g. piped/CI): only print on completion to avoid spam.
                if pct >= 100.0:
                    self.stream.write(self._line(index) + "\n")
                    self.stream.flush()

    def _repaint_locked(self) -> None:
        self.stream.write(f"\033[{self.n}A")  # cursor up N lines to the block start
        for i in range(self.n):
            self.stream.write("\r\033[K" + self._line(i) + "\n")
        self.stream.flush()

    def _line(self, i: int) -> str:
        pct = self.pcts[i]
        filled = int(round(self.width * pct / 100.0))
        bar = "#" * filled + "-" * (self.width - filled)
        label = self.labels[i].ljust(self._label_w)
        return f"{label} |{bar}| {pct:5.1f}%  {self.msgs[i]}"

    def close(self) -> None:
        with self._lock:
            if self._tty and self._started:
                self._repaint_locked()
            self.stream.flush()
