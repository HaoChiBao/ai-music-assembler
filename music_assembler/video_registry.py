"""A simple on-disk registry of generated music videos and their upload status.

Stored as one JSON object per line (JSON-lines) in a ``.txt`` file so it stays both
human-readable and easy to parse/update. ``generate-music-videos`` appends a ``pending``
entry per built video; ``schedule-music-videos`` reads the pending ones, uploads/schedules
each, and flips them to ``uploaded`` with the YouTube id and scheduled publish time.

Fields per entry:
    id            local video id (the run folder/basename, e.g. mv_20260617_001501_00)
    dir           absolute path to the video's folder
    video         path to the .mp4
    thumbnail     path to the thumbnail .png (or "")
    title         generated YouTube title
    description   path to the description .txt
    status        "pending" | "uploaded"
    youtube_id    YouTube video id once uploaded (or "")
    youtube_url   watch URL once uploaded (or "")
    publish_at    scheduled publish time, RFC3339 UTC (or "")
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_REGISTRY_FILE = Path("music-video/video_registry.txt")

STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"


@dataclass
class VideoEntry:
    id: str
    dir: str = ""
    video: str = ""
    thumbnail: str = ""
    title: str = ""
    description: str = ""
    status: str = STATUS_PENDING
    youtube_id: str = ""
    youtube_url: str = ""
    publish_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "VideoEntry":
        fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: d[k] for k in fields if k in d}
        # Merge any stored ``extra`` dict with unknown top-level keys (no double-nesting).
        extra = dict(kwargs.pop("extra", {}) or {})
        for k, v in d.items():
            if k not in fields:
                extra[k] = v
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)


class VideoRegistry:
    """Read/append/update the JSON-lines registry file (keyed by entry ``id``)."""

    def __init__(self, path: Path = DEFAULT_REGISTRY_FILE) -> None:
        self.path = Path(path)

    def load(self) -> list[VideoEntry]:
        if not self.path.is_file():
            return []
        entries: list[VideoEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(VideoEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries

    def _write_all(self, entries: list[VideoEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(e.to_json() + "\n" for e in entries), encoding="utf-8"
        )

    def append(self, entry: VideoEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(entry.to_json() + "\n")

    def pending(self) -> list[VideoEntry]:
        return [e for e in self.load() if e.status == STATUS_PENDING]

    def update(self, entry: VideoEntry) -> None:
        """Replace the entry with the same ``id`` (append if not present)."""
        entries = self.load()
        for i, e in enumerate(entries):
            if e.id == entry.id:
                entries[i] = entry
                self._write_all(entries)
                return
        self.append(entry)

    def mark_uploaded(
        self, entry_id: str, *, youtube_id: str, publish_at: str = ""
    ) -> None:
        entries = self.load()
        for e in entries:
            if e.id == entry_id:
                e.status = STATUS_UPLOADED
                e.youtube_id = youtube_id
                e.youtube_url = f"https://youtu.be/{youtube_id}" if youtube_id else ""
                if publish_at:
                    e.publish_at = publish_at
                self._write_all(entries)
                return
