"""
Extend photos to widescreen backgrounds with the Gemini API (image-capable models such as
**Gemini** image models (default: **gemini-3-pro-image-preview**).

Flow: load **prompts/background_master.txt**, open each source image, call **generate_content**
with text + image, save the first returned image (optionally resized to **~1600px** wide). No composites or masks.

Requires ``GEMINI_API_KEY`` in the environment (e.g. from ``.env``).
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image as PILImage

from music_assembler import __version__

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP"}

DEFAULT_MODEL = "gemini-3-pro-image-preview"
DEFAULT_ASPECT_RATIO = "16:9"
# API buckets (see Gemini image docs): 3 Pro 16:9 is 1376×768 (1K) or 2752×1536 (2K). We ask for 2K
# then resize to ~1600px wide so the saved PNG matches the target width without upscaling from 1K.
DEFAULT_IMAGE_SIZE = "2K"
DEFAULT_OUTPUT_WIDTH = 1600


def _iter_response_parts(response) -> list:
    parts = getattr(response, "parts", None)
    if parts:
        return list(parts)
    out = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        if content and getattr(content, "parts", None):
            out.extend(content.parts)
    return out


def _to_pil_image(im) -> PILImage.Image:
    """Turn a response image into a Pillow image (SDK may return ``google.genai.types.Image``)."""
    if isinstance(im, PILImage.Image):
        return im
    data = getattr(im, "image_bytes", None)
    if data:
        return PILImage.open(io.BytesIO(data)).convert("RGB")
    raise TypeError(f"Cannot convert image part to Pillow Image: {type(im)!r}")


def _response_debug_text(response) -> str:
    lines: list[str] = []
    for part in _iter_response_parts(response):
        t = getattr(part, "text", None)
        if t:
            lines.append(t[:2000])
    return "\n".join(lines) if lines else "(no text parts)"


def _save_first_image(
    response,
    out_path: Path,
    *,
    output_width: int | None,
) -> bool:
    for part in _iter_response_parts(response):
        raw = part.as_image()
        if raw is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            im = _to_pil_image(raw)
            if output_width is not None and output_width > 0:
                w, h = im.size
                if w != output_width:
                    nh = max(1, round(h * output_width / w))
                    im = im.resize((output_width, nh), PILImage.Resampling.LANCZOS)
            im.save(out_path)
            return True
    return False


def _load_prompt(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Prompt file is empty: {path}")
    return text


def _discover_images(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    files = sorted(p for p in input_dir.iterdir() if p.suffix in IMAGE_EXTS and p.is_file())
    if not files:
        raise ValueError(f"No images found in {input_dir} (supported: {', '.join(sorted(IMAGE_EXTS))})")
    return files


def _text_preview(s: str, limit: int = 800) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def extend_one(
    *,
    client,
    model: str,
    prompt: str,
    image_path: Path,
    out_path: Path,
    aspect_ratio: str,
    image_size: str,
    output_width: int | None,
) -> None:
    img = PILImage.open(image_path).convert("RGB")
    from google.genai import types

    config = types.GenerateContentConfig(
        response_modalities=["TEXT", "IMAGE"],
        image_config=types.ImageConfig(
            aspect_ratio=aspect_ratio,
            image_size=image_size,
        ),
    )
    response = client.models.generate_content(
        model=model,
        contents=[prompt, img],
        config=config,
    )
    if not _save_first_image(response, out_path, output_width=output_width):
        fb = getattr(response, "prompt_feedback", None)
        dbg = _response_debug_text(response)
        extra = f" prompt_feedback={fb}" if fb else ""
        raise RuntimeError(
            f"No image in model response for {image_path.name}.{extra}\n{_text_preview(dbg)}"
        )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="extend-backgrounds",
        description=(
            "Send each photo in pre-processed/ to Gemini (image model) with the master prompt; "
            "save PNGs to post-processed/."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("pre-processed"),
        help="Folder of source photos (default: pre-processed).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("post-processed"),
        help="Folder for generated images (default: post-processed).",
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("prompts/background_master.txt"),
        help="Master prompt text file.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
        help=f"Gemini image model id (default: {DEFAULT_MODEL} or GEMINI_IMAGE_MODEL).",
    )
    parser.add_argument(
        "--aspect-ratio",
        default=os.environ.get("GEMINI_ASPECT_RATIO", DEFAULT_ASPECT_RATIO),
        help=f"Output aspect ratio for image_config (default: {DEFAULT_ASPECT_RATIO} or GEMINI_ASPECT_RATIO).",
    )
    parser.add_argument(
        "--image-size",
        default=os.environ.get("GEMINI_IMAGE_SIZE", DEFAULT_IMAGE_SIZE),
        choices=("512", "1K", "2K", "4K"),
        help=(
            "Gemini image_config.image_size: 512, 1K, 2K, or 4K "
            f"(default: {DEFAULT_IMAGE_SIZE} or GEMINI_IMAGE_SIZE)."
        ),
    )
    parser.add_argument(
        "--output-width",
        type=int,
        default=int(os.environ.get("GEMINI_OUTPUT_WIDTH", str(DEFAULT_OUTPUT_WIDTH))),
        help=(
            "Resize saved image to this width (px), preserving aspect ratio. "
            f"Default {DEFAULT_OUTPUT_WIDTH}. Use 0 for native API dimensions."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files in the output directory.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N images (for testing).",
    )
    args = parser.parse_args(argv)
    out_w = args.output_width if args.output_width > 0 else None

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(
            "Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment. Add it to .env.",
            file=sys.stderr,
        )
        return 2

    try:
        from google import genai
    except ImportError as e:
        print("Install dependencies: pip install google-genai python-dotenv Pillow", file=sys.stderr)
        raise SystemExit(2) from e

    prompt = _load_prompt(args.prompt_file)
    images = _discover_images(args.input_dir.resolve())
    if args.limit is not None:
        images = images[: max(0, args.limit)]

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)
    ok = 0
    skipped = 0
    for src in images:
        out_name = src.stem + ".png"
        dest = out_dir / out_name
        if dest.is_file() and not args.force:
            print(f"skip (exists): {dest.name}")
            skipped += 1
            continue
        print(
            f"extend: {src.name} -> {dest.name} "
            f"(model={args.model}, image_size={args.image_size}, output_width={out_w or 'native'})"
        )
        try:
            extend_one(
                client=client,
                model=args.model,
                prompt=prompt,
                image_path=src,
                out_path=dest,
                aspect_ratio=args.aspect_ratio,
                image_size=args.image_size,
                output_width=out_w,
            )
            ok += 1
        except Exception as e:
            print(f"error: {src.name}: {e}", file=sys.stderr)
            return 1

    print(f"Done. wrote {ok}, skipped {skipped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
