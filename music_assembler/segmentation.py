"""Subject segmentation backends for the text-behind-subject effect.

Two backends produce an ``L`` mask (255 = subject/foreground, 0 = background):

* ``rembg``  — local U^2-Net model (offline after first download).
* ``gemini`` — Google Gemini 2.5 segmentation via ``google-genai`` (needs GEMINI_API_KEY).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re

from PIL import Image, ImageDraw, ImageFilter

# Cached rembg sessions, keyed by model name, so a batch reuses one loaded model.
_REMBG_SESSIONS: dict[str, object] = {}

DEFAULT_REMBG_MODEL = "isnet-general-use"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_SUBJECT_PROMPT = "the main subject (the person or central object in the foreground)"

_SEG_PROMPT_TEMPLATE = (
    "Give the segmentation masks for {subject}.\n"
    "Output a JSON list of segmentation masks where each entry contains the 2D "
    'bounding box in the key "box_2d", the segmentation mask in the key "mask", '
    'and the text label in the key "label". Use descriptive labels.'
)


def segment_rembg(
    image: Image.Image,
    *,
    model: str = DEFAULT_REMBG_MODEL,
    alpha_matting: bool = False,
    alpha_matting_foreground_threshold: int = 240,
    alpha_matting_background_threshold: int = 10,
    alpha_matting_erode_size: int = 10,
) -> Image.Image:
    """Subject mask via ``rembg``. Raises if the package is missing.

    ``model`` picks the network. Higher-detail options (better hair/edges) include
    ``birefnet-general`` (default), ``isnet-general-use``, and ``u2net_human_seg`` for
    people; ``u2net`` is the smaller original. ``alpha_matting`` softens fine edges
    (slower) using the threshold/erode parameters.
    """
    try:
        from rembg import new_session, remove
    except ImportError as e:  # pragma: no cover - optional install
        raise RuntimeError(
            "The 'rembg' backend needs the 'rembg' package. Install it with:\n"
            '    pip install "rembg[cpu]"\n'
            "(or the project extra: pip install '.[segmentation]')."
        ) from e

    if model not in _REMBG_SESSIONS:
        _REMBG_SESSIONS[model] = new_session(model)

    mask = remove(
        image.convert("RGB"),
        session=_REMBG_SESSIONS[model],
        only_mask=True,
        alpha_matting=alpha_matting,
        alpha_matting_foreground_threshold=alpha_matting_foreground_threshold,
        alpha_matting_background_threshold=alpha_matting_background_threshold,
        alpha_matting_erode_size=alpha_matting_erode_size,
        post_process_mask=True,
    )
    return mask.convert("L")


def refine_mask(mask: Image.Image, *, shrink_px: int = 0, feather_px: float = 0.0) -> Image.Image:
    """Clean up a hard subject mask for more natural compositing.

    ``shrink_px`` erodes the subject edge inward (removes the background ``halo``/fringe
    left by imperfect cutouts). ``feather_px`` then Gaussian-blurs the mask so edges fade
    softly into the layers behind, instead of looking hand-cut.
    """
    out = mask.convert("L")
    if shrink_px and shrink_px > 0:
        # MinFilter shrinks the white (subject) region; kernel must be odd.
        k = 2 * shrink_px + 1
        out = out.filter(ImageFilter.MinFilter(k))
    if feather_px and feather_px > 0:
        out = out.filter(ImageFilter.GaussianBlur(radius=feather_px))
    return out


def _strip_json_fence(text: str) -> str:
    """Remove ```json fences / leading prose so ``json.loads`` sees a bare list."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()
    start = t.find("[")
    end = t.rfind("]")
    if start != -1 and end != -1 and end > start:
        return t[start : end + 1]
    return t


def _decode_mask_png(b64: str) -> Image.Image:
    """Decode a base64 PNG mask (tolerating a ``data:image/png;base64,`` prefix)."""
    b64 = b64.strip()
    if b64.lower().startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1].strip()
    # Restore stripped ``=`` padding so the length is a multiple of 4.
    pad = len(b64) % 4
    if pad:
        b64 += "=" * (4 - pad)
    try:
        raw = base64.b64decode(b64)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"Gemini returned an undecodable mask: {e}") from e
    return Image.open(io.BytesIO(raw)).convert("L")


def segment_gemini(
    image: Image.Image,
    *,
    subject: str = DEFAULT_SUBJECT_PROMPT,
    model: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
    threshold: int = 127,
) -> Image.Image:
    """Subject mask via Gemini 2.5 segmentation.

    Gemini returns a JSON list; each entry has ``box_2d`` ([y0, x0, y1, x1] normalized
    0-1000), a base64 PNG ``mask`` (0-255 probability map within the box), and a ``label``.
    Each mask is resized to its box, binarized at ``threshold``, and OR-ed into a
    full-size ``L`` mask.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "The 'gemini' backend needs GEMINI_API_KEY (or GOOGLE_API_KEY) in the "
            "environment. Add it to .env."
        )
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:  # pragma: no cover - optional install
        raise RuntimeError(
            "The 'gemini' backend needs google-genai: pip install google-genai"
        ) from e

    rgb = image.convert("RGB")
    width, height = rgb.size

    client = genai.Client(api_key=key)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        # Mask polygons can be thousands of tokens; avoid truncated (invalid) JSON.
        max_output_tokens=65536,
    )
    response = client.models.generate_content(
        model=model,
        contents=[_SEG_PROMPT_TEMPLATE.format(subject=subject), rgb],
        config=config,
    )

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned no text for the segmentation request.")
    try:
        items = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse Gemini segmentation JSON ({e}). The model returned "
            "malformed output for this request; try a different --gemini-model or "
            "use --segmenter rembg."
        ) from e
    if not isinstance(items, list) or not items:
        raise RuntimeError("Gemini returned no segmentation masks for the requested subject.")

    full = Image.new("L", (width, height), 0)
    used = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        mask = item.get("mask")
        if isinstance(mask, str) and mask.strip():
            # Documented format: base64 PNG probability map within box_2d.
            box = item.get("box_2d")
            if not box or len(box) != 4:
                continue
            y0, x0, y1, x1 = box
            px0 = max(0, min(width, round(x0 / 1000 * width)))
            py0 = max(0, min(height, round(y0 / 1000 * height)))
            px1 = max(0, min(width, round(x1 / 1000 * width)))
            py1 = max(0, min(height, round(y1 / 1000 * height)))
            bw, bh = px1 - px0, py1 - py0
            if bw <= 0 or bh <= 0:
                continue
            try:
                region = _decode_mask_png(mask).resize((bw, bh), Image.Resampling.BILINEAR)
            except (RuntimeError, OSError, ValueError):
                # Some model versions put prose/tokens here instead of a base64 PNG; skip it.
                continue
            binar = region.point(lambda p: 255 if p >= threshold else 0)
            full.paste(255, (px0, py0, px1, py1), binar)
            used += 1
        elif isinstance(mask, list) and mask:
            # Newer models may return polygon outlines ([y, x, ...] normalized 0-1000).
            if _rasterize_polygons(full, mask, width, height):
                used += 1

    if used == 0:
        raise RuntimeError(
            "Gemini returned masks but none were usable (model output format not "
            "recognized). Try --segmenter rembg, or a different --gemini-model."
        )
    return full


def _flat_to_points(flat: list, width: int, height: int) -> list[tuple[float, float]]:
    """``[y, x, y, x, ...]`` (normalized 0-1000) -> pixel ``(x, y)`` points."""
    return [
        (flat[i + 1] / 1000 * width, flat[i] / 1000 * height)
        for i in range(0, len(flat) - 1, 2)
        if isinstance(flat[i], (int, float)) and isinstance(flat[i + 1], (int, float))
    ]


def _rasterize_polygons(full: Image.Image, mask: list, width: int, height: int) -> bool:
    """Fill polygon outline(s) into ``full``.

    Gemini models return mask outlines in several shapes (all normalized 0-1000, ``[y, x]``):
    a flat list ``[y, x, ...]``; a list of point pairs ``[[y, x], ...]``; or a list of such
    polygons. This normalizes all of them to filled polygons.
    """
    inner_lists = [el for el in mask if isinstance(el, (list, tuple))]
    polygons: list[list[tuple[float, float]]] = []

    if inner_lists and len(inner_lists) == len(mask):
        if all(len(el) == 2 for el in inner_lists):
            # Single polygon expressed as [y, x] point pairs.
            polygons.append([
                (el[1] / 1000 * width, el[0] / 1000 * height) for el in inner_lists
            ])
        else:
            # One or more polygons, each a flat [y, x, ...] list.
            polygons.extend(_flat_to_points(list(el), width, height) for el in inner_lists)
    elif all(isinstance(el, (int, float)) for el in mask):
        polygons.append(_flat_to_points(mask, width, height))

    draw = ImageDraw.Draw(full)
    drew = False
    for pts in polygons:
        if len(pts) >= 3:
            draw.polygon(pts, fill=255)
            drew = True
    return drew


def segment_subject_mask(
    image: Image.Image,
    *,
    backend: str = "rembg",
    rembg_model: str = DEFAULT_REMBG_MODEL,
    alpha_matting: bool = False,
    subject: str = DEFAULT_SUBJECT_PROMPT,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
) -> Image.Image:
    """Dispatch to the requested segmentation ``backend`` (``rembg`` or ``gemini``)."""
    if backend == "rembg":
        return segment_rembg(image, model=rembg_model, alpha_matting=alpha_matting)
    if backend == "gemini":
        return segment_gemini(image, subject=subject, model=gemini_model, api_key=api_key)
    raise ValueError(f"Unknown segmentation backend: {backend!r} (use 'rembg' or 'gemini').")
