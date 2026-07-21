"""Registry of assembly video templates (product variants).

Each template defines the encode geometry, default mix length, title overlay,
thumbnail strategy, and background aspect ratio. The dashboard, schedules, and
Cloud Run workers all resolve a ``template_id`` through this module so new
variants can be added in one place.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from music_assembler.config import (
    DEFAULT_VIDEO_HEIGHT,
    DEFAULT_VIDEO_WIDTH,
    DurationBounds,
    TextOverlayStyle,
)

ThumbnailStrategy = Literal["text_behind_subject", "none"]

DEFAULT_TEMPLATE_ID = "playlist_landscape"
ENV_TEMPLATE_ID = "ASSEMBLY_TEMPLATE_ID"


@dataclass(frozen=True)
class VideoTemplate:
    """One assembly product shape (dimensions, defaults, thumbnail strategy)."""

    id: str
    name: str
    description: str
    video_width: int
    video_height: int
    default_duration_min: int
    default_variance_min: int
    default_thumbnail_text: str | None
    thumbnail_strategy: ThumbnailStrategy = "text_behind_subject"
    title_font_size_px: int = 46
    title_font_weight: int = 400
    title_fill_color: tuple[int, int, int, int] = (255, 255, 255, 235)
    gemini_aspect_ratio: str = "16:9"
    # Soft tags for UI filters / future routing (e.g. background pools).
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def aspect_label(self) -> str:
        w, h = self.video_width, self.video_height
        if w * 9 == h * 16:
            return "16:9"
        if w * 16 == h * 9:
            return "9:16"
        if w == h:
            return "1:1"
        return f"{w}:{h}"

    @property
    def orientation(self) -> str:
        if self.video_width > self.video_height:
            return "landscape"
        if self.video_height > self.video_width:
            return "portrait"
        return "square"

    def duration_bounds(self) -> DurationBounds:
        target = float(self.default_duration_min) * 60.0
        margin = float(self.default_variance_min) * 60.0
        lo = max(1.0, target - margin)
        hi = max(lo, target + margin)
        return DurationBounds(min_sec=lo, max_sec=hi)

    def text_overlay_style(self, *, font_key: str) -> TextOverlayStyle:
        return TextOverlayStyle(
            font_key=font_key,
            font_size_px=self.title_font_size_px,
            font_weight=self.title_font_weight,
            fill_color=self.title_fill_color,
        )

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe summary for the dashboard / API."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "video_width": self.video_width,
            "video_height": self.video_height,
            "aspect_label": self.aspect_label,
            "orientation": self.orientation,
            "default_duration_min": self.default_duration_min,
            "default_variance_min": self.default_variance_min,
            "default_thumbnail_text": self.default_thumbnail_text,
            "thumbnail_strategy": self.thumbnail_strategy,
            "title_font_size_px": self.title_font_size_px,
            "gemini_aspect_ratio": self.gemini_aspect_ratio,
            "tags": list(self.tags),
            "is_default": self.id == DEFAULT_TEMPLATE_ID,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aspect_label"] = self.aspect_label
        data["orientation"] = self.orientation
        return data


# ---------------------------------------------------------------------------
# Built-in templates — add new variants here.
# ---------------------------------------------------------------------------

PLAYLIST_LANDSCAPE = VideoTemplate(
    id="playlist_landscape",
    name="Playlist · Landscape",
    description="Long-form 16:9 still-image playlist with per-song titles and branded thumbnail.",
    video_width=DEFAULT_VIDEO_WIDTH,
    video_height=DEFAULT_VIDEO_HEIGHT,
    default_duration_min=90,
    default_variance_min=15,
    default_thumbnail_text="PLAYLIST",
    thumbnail_strategy="text_behind_subject",
    title_font_size_px=46,
    title_font_weight=400,
    gemini_aspect_ratio="16:9",
    tags=("playlist", "landscape", "youtube"),
)

SHORTS_VERTICAL = VideoTemplate(
    id="shorts_vertical",
    name="Shorts · Vertical",
    description="Short-form 9:16 vertical music video for YouTube Shorts / Reels-style posts.",
    video_width=1080,
    video_height=1920,
    default_duration_min=1,
    default_variance_min=0,
    default_thumbnail_text="SHORTS",
    thumbnail_strategy="text_behind_subject",
    title_font_size_px=52,
    title_font_weight=400,
    gemini_aspect_ratio="9:16",
    tags=("shorts", "vertical", "portrait"),
)

_TEMPLATES: dict[str, VideoTemplate] = {
    PLAYLIST_LANDSCAPE.id: PLAYLIST_LANDSCAPE,
    SHORTS_VERTICAL.id: SHORTS_VERTICAL,
}


class UnknownTemplateError(ValueError):
    """Raised when a template_id is not registered."""


def list_templates() -> list[VideoTemplate]:
    """All registered templates in stable display order."""
    # Default first, then the rest alphabetically by name.
    items = list(_TEMPLATES.values())
    items.sort(key=lambda t: (0 if t.id == DEFAULT_TEMPLATE_ID else 1, t.name.lower()))
    return items


def list_template_ids() -> list[str]:
    return [t.id for t in list_templates()]


def get_template(template_id: str | None) -> VideoTemplate:
    """Resolve a template id; blank/None falls back to the default.

    Raises ``UnknownTemplateError`` for unknown non-empty ids.
    """
    tid = normalize_template_id(template_id)
    tmpl = _TEMPLATES.get(tid)
    if tmpl is None:
        known = ", ".join(list_template_ids())
        raise UnknownTemplateError(f"Unknown video template {template_id!r}. Known: {known}")
    return tmpl


def normalize_template_id(template_id: str | None) -> str:
    """Return a canonical template id (default when blank)."""
    if template_id is None:
        return DEFAULT_TEMPLATE_ID
    tid = str(template_id).strip().lower().replace(" ", "_")
    if not tid:
        return DEFAULT_TEMPLATE_ID
    return tid


def resolve_template_id(template_id: str | None = None) -> str:
    """CLI/API/env resolution: explicit arg → ``ASSEMBLY_TEMPLATE_ID`` → default."""
    if template_id is not None and str(template_id).strip():
        return get_template(template_id).id
    env = os.environ.get(ENV_TEMPLATE_ID, "").strip()
    if env:
        return get_template(env).id
    return DEFAULT_TEMPLATE_ID


def resolve_template(template_id: str | None = None) -> VideoTemplate:
    return get_template(resolve_template_id(template_id))


def templates_public_list() -> list[dict[str, Any]]:
    return [t.to_public_dict() for t in list_templates()]
