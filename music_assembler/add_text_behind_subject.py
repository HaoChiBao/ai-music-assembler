"""CLI: draw large text behind the subject of an image.

Picks a random image from ``post-processed/`` (or a specific ``--input``),
segments the subject from the background into two layers, draws big text
spanning the background, composites the subject back on top, and writes the
result to ``layer-text-image/``.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import discover_font_stems
from music_assembler.segmentation import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_REMBG_MODEL,
    DEFAULT_SUBJECT_PROMPT,
)
from music_assembler.text_behind_subject import _hex_to_rgba, render_text_behind_subject

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP"}

DEFAULT_FONT_WEIGHT = 700
DEFAULT_FILL = "#FFFFFF"
DEFAULT_WIDTH_FRAC = 0.92
DEFAULT_HEIGHT_FRAC = 0.6


def _discover_images(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    return sorted(p for p in input_dir.iterdir() if p.suffix in IMAGE_EXTS and p.is_file())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="add-text-behind-subject",
        description=(
            "Separate an image's subject from its background and draw large text behind "
            "the subject. Uses a random image from post-processed/ unless --input is given; "
            "writes a PNG to layer-text-image/."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--list-fonts",
        action="store_true",
        help="List all font face stems under fonts/ and exit.",
    )
    parser.add_argument(
        "--text",
        "-t",
        default=None,
        help="Text to place behind the subject (use \\n for multiple lines). Required unless --list-fonts.",
    )
    parser.add_argument(
        "--input",
        "-i",
        type=Path,
        default=None,
        help="Specific source image. Default: a random image from --input-dir.",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("post-processed"),
        help="Folder to pick a random image from (default: post-processed).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("layer-text-image"),
        help="Output folder (default: layer-text-image).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Explicit output PNG path (overrides --output-dir naming).",
    )
    parser.add_argument(
        "--font",
        default=None,
        metavar="STEM",
        help="Face stem in fonts/ (run --list-fonts to see options). Default: bundled font for the weight.",
    )
    parser.add_argument(
        "--font-weight",
        type=int,
        default=DEFAULT_FONT_WEIGHT,
        metavar="N",
        help=f"CSS-style weight when no exact --font (300=Light, 400=Regular, 700=Bold). Default: {DEFAULT_FONT_WEIGHT}.",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=None,
        help="Fixed font size in pixels. Default: auto-fit so text spans the image width.",
    )
    parser.add_argument(
        "--color",
        default=DEFAULT_FILL,
        help=f"Text fill color as #RRGGBB or #RRGGBBAA (default: {DEFAULT_FILL}).",
    )
    parser.add_argument(
        "--stroke-width",
        type=int,
        default=0,
        help="Outline thickness in pixels (0 = none).",
    )
    parser.add_argument(
        "--stroke-color",
        default="#000000",
        help="Outline color as #RRGGBB or #RRGGBBAA (default: #000000).",
    )
    parser.add_argument(
        "--embolden",
        type=int,
        default=0,
        help="Simulated extra weight: extra fill passes at small offsets (0 = none).",
    )
    parser.add_argument(
        "--width-frac",
        type=float,
        default=DEFAULT_WIDTH_FRAC,
        help=f"Fraction of image width the auto-fit text should span (default: {DEFAULT_WIDTH_FRAC}).",
    )
    parser.add_argument(
        "--height-frac",
        type=float,
        default=DEFAULT_HEIGHT_FRAC,
        help=f"Max fraction of image height the text block may use (default: {DEFAULT_HEIGHT_FRAC}).",
    )
    parser.add_argument(
        "--v-align",
        choices=("top", "center", "bottom"),
        default="top",
        help=(
            "Vertical placement of the text block (default: top). For 'top'/'bottom' the "
            "gap from that edge matches the horizontal side gap."
        ),
    )
    parser.add_argument(
        "--segmenter",
        choices=("rembg", "gemini"),
        default="rembg",
        help=(
            "Subject segmentation backend: 'rembg' (local U^2-Net, default) or "
            "'gemini' (Google Gemini 2.5, needs GEMINI_API_KEY)."
        ),
    )
    parser.add_argument(
        "--subject-prompt",
        default=DEFAULT_SUBJECT_PROMPT,
        help="What to segment (gemini backend only). Default targets the main foreground subject.",
    )
    parser.add_argument(
        "--gemini-model",
        default=DEFAULT_GEMINI_MODEL,
        help=f"Gemini model for segmentation (default: {DEFAULT_GEMINI_MODEL}).",
    )
    parser.add_argument(
        "--rembg-model",
        default=DEFAULT_REMBG_MODEL,
        help=(
            "rembg model (rembg backend only). Default 'isnet-general-use' (finer edges). "
            "Best hair: 'birefnet-general' (~1GB, slow on CPU). People: 'u2net_human_seg'. "
            "Smaller/faster: 'u2net'. Non-cached models download on first use."
        ),
    )
    parser.add_argument(
        "--alpha-matting",
        action="store_true",
        help="Refine subject edges with alpha matting (rembg backend only; slower, softer hair edges).",
    )
    parser.add_argument(
        "--feather",
        type=float,
        default=1.5,
        metavar="PX",
        help=(
            "Gaussian blur radius applied to the subject mask edge so it blends naturally "
            "(0 = hard edge). Default: 1.5. Try 2-4 for softer hair."
        ),
    )
    parser.add_argument(
        "--shrink",
        type=int,
        default=1,
        metavar="PX",
        help=(
            "Erode the subject edge inward by N px before feathering, to remove the "
            "background halo/fringe from imperfect cutouts (default: 1)."
        ),
    )
    parser.add_argument(
        "--text-opacity",
        type=int,
        default=100,
        metavar="PCT",
        help=(
            "Text fill opacity 0-100 (default: 100). Lower values (e.g. 80-90) let the "
            "background show through so the text feels more integrated."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed for the random image pick (reproducible output).",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root for fonts/ (default: current directory).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print which TrueType font PIL loaded (stderr).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.project_root.resolve()

    if args.list_fonts:
        for stem in discover_font_stems(root):
            print(stem)
        return 0

    if args.text is None:
        parser.error("--text is required unless you pass --list-fonts")

    text = args.text.replace("\\n", "\n")

    try:
        fill_color = _hex_to_rgba(args.color)
        stroke_color = _hex_to_rgba(args.stroke_color)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    opacity = max(0, min(100, args.text_opacity))
    if opacity < 100:
        fill_color = (*fill_color[:3], round(fill_color[3] * opacity / 100))

    if args.input is not None:
        src = args.input.resolve()
        if not src.is_file():
            print(f"Input not found: {src}", file=sys.stderr)
            return 2
    else:
        try:
            images = _discover_images(args.input_dir.resolve())
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 2
        if not images:
            print(f"No images in {args.input_dir}", file=sys.stderr)
            return 2
        rng = random.Random(args.seed)
        src = rng.choice(images)

    if args.output is not None:
        dest = args.output.resolve()
    else:
        dest = (args.output_dir.resolve() / (src.stem + "_behind.png"))

    if dest.is_file() and not args.force:
        print(f"skip (exists): {dest} (use --force to overwrite)")
        return 0

    font_key = resolve_font_key(root, args.font, weight=args.font_weight)
    print(f"{src.name} -> {dest}")

    try:
        render_text_behind_subject(
            src,
            text,
            dest,
            font_key=font_key,
            font_weight=args.font_weight,
            font_size_px=args.font_size,
            fill_color=fill_color,
            stroke_width=args.stroke_width,
            stroke_color=stroke_color,
            embolden=args.embolden,
            width_frac=args.width_frac,
            height_frac=args.height_frac,
            vertical=args.v_align,
            segmenter=args.segmenter,
            rembg_model=args.rembg_model,
            alpha_matting=args.alpha_matting,
            feather_px=args.feather,
            mask_shrink_px=args.shrink,
            subject_prompt=args.subject_prompt,
            gemini_model=args.gemini_model,
            project_root=root,
            log_font_load=not args.quiet,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {src.name}: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
