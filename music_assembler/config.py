"""Default durations, paths, and font resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


# Target mix length in seconds (1h15m .. 1h45m by default).
DEFAULT_MIN_DURATION_SEC = 75 * 60
DEFAULT_MAX_DURATION_SEC = 105 * 60

# Video: common horizontal sizes (16:9).
DEFAULT_VIDEO_WIDTH = 1920
DEFAULT_VIDEO_HEIGHT = 1080

# Paths relative to project root when using discover_fonts().
FONTS_DIR_NAME = "fonts"

# Default light face: this file under ``fonts/`` (used when no ``--font`` and weight is Light).
DEFAULT_BUNDLED_LIGHT_FONT_REL = "Inria_Serif/InriaSerif-Light.ttf"
DEFAULT_BUNDLED_LIGHT_FONT_STEM = Path(DEFAULT_BUNDLED_LIGHT_FONT_REL).stem

# macOS system fonts often used for clean overlays (tried in order per key).
_FONT_CANDIDATES: Mapping[str, tuple[str, ...]] = {
    "sf_pro": (
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ),
    "arial": (
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ),
    "helvetica": (
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Helvetica.ttf",
    ),
    "times": (
        "/Library/Fonts/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    ),
    "georgia": (
        "/Library/Fonts/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
    ),
}


@dataclass
class AssemblerPaths:
    """Folders for inputs and outputs."""

    songs_dir: Path
    images_dir: Path
    fonts_dir: Path | None = None
    output_dir: Path = Path("output")
    # Used to resolve fonts/ and defaults; set if you run the CLI from another cwd.
    project_root: Path = field(default_factory=lambda: Path.cwd())


@dataclass
class DurationBounds:
    min_sec: float = DEFAULT_MIN_DURATION_SEC
    max_sec: float = DEFAULT_MAX_DURATION_SEC

    def __post_init__(self) -> None:
        if self.min_sec <= 0 or self.max_sec <= 0:
            raise ValueError("Durations must be positive.")
        if self.min_sec > self.max_sec:
            raise ValueError("min_sec must be <= max_sec.")


@dataclass
class TextOverlayStyle:
    """Figma-like text on image: position, size, color."""

    font_key: str = "arial"
    font_size_px: int = 72
    # CSS-style weight when resolving a file in fonts/ (300 = Light, 400 = Regular, 700 = Bold).
    # None = match font_key only (no weight suffix).
    font_weight: int | None = None
    fill_color: tuple[int, int, int, int] = (255, 255, 255, 255)
    stroke_width: int = 2
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 255)
    # Simulated weight: extra fill draws at small offsets (0 = lightest; 1–2 = heavier).
    embolden: int = 0
    # Anchor: horizontal "left" | "center" | "right", vertical "top" | "center" | "bottom"
    horizontal: str = "center"
    vertical: str = "center"
    margin_px: int = 48
    # When set and vertical is "bottom", distance from image bottom to text (overrides margin_px for Y only).
    margin_bottom_px: int | None = None
    line_spacing: float = 1.2


@dataclass
class AssemblerConfig:
    paths: AssemblerPaths
    duration: DurationBounds = field(default_factory=DurationBounds)
    text: TextOverlayStyle = field(default_factory=TextOverlayStyle)
    video_width: int = DEFAULT_VIDEO_WIDTH
    video_height: int = DEFAULT_VIDEO_HEIGHT
    seed: int | None = None


def _stem_key(path: Path) -> str:
    return path.stem.lower().replace(" ", "_")


def _weight_substrings(weight: int) -> tuple[str, ...]:
    """Filename tokens typical for OpenType/CSS weights."""
    if weight <= 220:
        return ("thin", "hairline", "extralight", "extra-light", "100", "200")
    if weight <= 320:
        return ("light", "300")
    if weight <= 450:
        return ("regular", "normal", "book", "400")
    if weight <= 550:
        return ("medium", "500")
    if weight <= 650:
        return ("semibold", "semi-bold", "demi", "600")
    return ("bold", "700", "heavy", "black", "900")


def _family_compact(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _resolve_font_in_fonts_dir(
    fonts_dir: Path,
    font_key: str,
    weight: int | None,
) -> Path | None:
    """Match bundled fonts; if ``weight`` is set, prefer files whose stem matches that weight."""
    key = font_key.lower().strip().replace(" ", "_")
    key_compact = _family_compact(key)

    def all_font_files() -> list[Path]:
        return sorted(
            p
            for p in fonts_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in (".ttf", ".otf")
        )

    # 1) Exact stem match
    for p in all_font_files():
        if _stem_key(p) == key:
            return p

    # 2) Weight-aware: stems like Family-Light, Family_300, etc.
    if weight is not None:
        subs = _weight_substrings(weight)
        candidates: list[Path] = []
        for p in all_font_files():
            stem = _stem_key(p)
            stem_c = _family_compact(stem)
            if key_compact and key_compact not in stem_c and not stem.startswith(key):
                continue
            low = stem.lower()
            if any(sub in low for sub in subs):
                candidates.append(p)
        if candidates:
            return candidates[0]

    # 3) Substring fallback (original behavior)
    for p in all_font_files():
        if key in _stem_key(p):
            return p
    return None


def resolve_font_path(
    font_key: str,
    project_root: Path | None = None,
    *,
    weight: int | None = None,
) -> Path | None:
    """
    Resolve a logical font key to a .ttf/.otf path.
    Checks bundled fonts dir (optionally preferring a weight such as 300 = Light), then system candidates.
    """
    key = font_key.lower().strip()
    fonts_dir = (project_root or Path.cwd()) / FONTS_DIR_NAME
    if fonts_dir.is_dir():
        found = _resolve_font_in_fonts_dir(fonts_dir, font_key, weight)
        if found is not None:
            return found

    for candidate in _FONT_CANDIDATES.get(key, ()):
        cp = Path(candidate)
        if cp.is_file():
            return cp
    return None


def discover_font_stems(project_root: Path | None = None) -> list[str]:
    """Sorted face stems (``Path.stem``) for every ``.ttf`` / ``.otf`` under ``fonts/``."""
    fonts_dir = (project_root or Path.cwd()) / FONTS_DIR_NAME
    if not fonts_dir.is_dir():
        return []
    seen: set[str] = set()
    out: list[str] = []
    for p in sorted(
        x
        for x in fonts_dir.rglob("*")
        if x.is_file() and x.suffix.lower() in (".ttf", ".otf")
    ):
        s = p.stem
        if s not in seen:
            seen.add(s)
            out.append(s)
    return sorted(out, key=str.lower)


def default_font_stem(project_root: Path, weight: int | None = 300) -> str | None:
    """
    Prefer bundled **InriaSerif-Light** when ``weight`` is unset or light (below 350),
    then **InriaSerif-Regular** when ``weight`` is in the regular/medium band (350–550),
    else the first face matching ``weight``.
    """
    fonts_dir = project_root / FONTS_DIR_NAME
    if not fonts_dir.is_dir():
        return None
    w = 300 if weight is None else weight
    if w < 350:
        p = fonts_dir / DEFAULT_BUNDLED_LIGHT_FONT_REL
        if p.is_file():
            return p.stem
        p_otf = fonts_dir / "Inria_Serif/InriaSerif-Light.otf"
        if p_otf.is_file():
            return p_otf.stem
        return first_font_stem_in_project(project_root, weight=weight or 300)
    if 350 <= w <= 550:
        for rel in ("Inria_Serif/InriaSerif-Regular.ttf", "Inria_Serif/InriaSerif-Regular.otf"):
            p = fonts_dir / rel
            if p.is_file():
                return p.stem
    return first_font_stem_in_project(project_root, weight=weight)


def first_font_stem_in_project(project_root: Path, weight: int | None = None) -> str | None:
    """Stem key for the first ``.ttf``/``.otf`` in ``fonts/``; if ``weight`` is set, prefer a matching file."""
    fonts_dir = (project_root or Path.cwd()) / FONTS_DIR_NAME
    if not fonts_dir.is_dir():
        return None
    paths = sorted(
        p
        for p in fonts_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".ttf", ".otf")
    )
    if weight is not None:
        subs = _weight_substrings(weight)
        for p in paths:
            stem = _stem_key(p)
            if any(sub in stem for sub in subs):
                return stem
    if paths:
        return _stem_key(paths[0])
    return None


def list_font_keys(project_root: Path | None = None) -> list[str]:
    """Keys available: built-in names plus stems of files in fonts/."""
    keys: set[str] = set(_FONT_CANDIDATES.keys())
    root = project_root or Path.cwd()
    d = root / FONTS_DIR_NAME
    if d.is_dir():
        for p in d.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf"):
                keys.add(p.stem.lower().replace(" ", "_"))
    return sorted(keys)
