# Music video assembler

Build **long MP3 mixes** from a library of tracks, draw **title text** on a **horizontal still image** (similar to exporting a frame from Figma), and **mux** everything into a single **MP4**: one static video with your audio underneath.

## What it does

1. **Audio mix** — Scans a folder of MP3s, shuffles and concatenates tracks until the total length is at least your minimum duration, then trims to your maximum if needed (default **75–105 minutes**; fully configurable).
2. **Image + text** — Loads a background image (PNG/JPEG/WebP), renders multi-line text with a chosen font, fill color, and outline/stroke.
3. **Video** — Encodes a **1920×1080** (configurable) H.264 + AAC MP4 with the still image looped for the full audio length.

## Requirements

- **Python 3.10+**
- **FFmpeg** and **ffprobe** on your `PATH` (e.g. macOS: `brew install ffmpeg`)
- **Pillow**, **google-genai**, **python-dotenv** (installed with the project)
- **Gemini API key** — For the optional **background extension** step only (`GEMINI_API_KEY` in `.env`). The main assembler only needs FFmpeg + images.

## Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python3 -m pip install --upgrade pip
python3 -m pip install .
```

Re-install after you change project code: **`python3 -m pip install .`**

Alternatively:

```bash
python3 -m pip install -r requirements.txt
```

Avoid **`pip install -e .`** on **Python 3.14+** with current setuptools: editable installs use a **`__editable__*.pth`** file that Python’s **`site`** module may skip as “hidden,” so **`music_assembler`** is not importable unless your current directory is the repo (console scripts then fail with `ModuleNotFoundError`). A normal **`pip install .`** copies the package into **`site-packages`** and fixes this.

This installs the same packages as `pyproject.toml` and registers:

- `assemble-music-video` — full mix + titled still + MP4  
- `extend-backgrounds` — pre-processed photos → widescreen post-processed backgrounds (Gemini **image** models; default **gemini-3-pro-image-preview**)  
- `extend-first-three` — same as `extend-backgrounds --limit 3` (handy for quick tests)  
- `add-bottom-text` — overlay caption text on **one** image; placement and outline are configurable (uses **`fonts/`** when present)  
- `add-text` — same text on the **first three** images in **`post-processed/`** → **`post-text-processed/`** (defaults: **96px**, **no outline**, **bottom center**)  

### Environment variables (`.env`)

1. Copy the example file:  
   `cp .env.example .env`
2. Edit **`.env`** (never commit it; it’s in `.gitignore`).

| Variable | Required | Used by |
|----------|----------|---------|
| `GEMINI_API_KEY` | For **`extend-backgrounds` only** | [Google AI Studio](https://aistudio.google.com/apikey) (same key works with the Gemini API) |
| `GEMINI_IMAGE_MODEL` | No | Image model id (default: `gemini-3-pro-image-preview`). Use another id from the [Gemini API model list](https://ai.google.dev/gemini-api/docs/models) if you prefer (e.g. `gemini-2.5-flash-image`). |
| `GEMINI_ASPECT_RATIO` | No | Output aspect ratio for the image config (default: `16:9`) |
| `GEMINI_IMAGE_SIZE` | No | API resolution bucket: `512`, `1K`, `2K`, or `4K` (default: **`2K`**). See [image generation](https://ai.google.dev/gemini-api/docs/image-generation) for pixel sizes per ratio. |
| `GEMINI_OUTPUT_WIDTH` | No | Resize saved PNG to this width in pixels (default: **`1600`**; `0` = keep native API dimensions). |

`python-dotenv` loads `.env` when you run **`extend-backgrounds`**. **`assemble-music-video`** also loads `.env` if present so future options can live in one place.

If you only run the video assembler (no AI background step), you do **not** need any API keys—only FFmpeg and your images.

## Project layout

| Path | Purpose |
|------|---------|
| `music/` | **MP3 library** (or point `--songs-dir` anywhere). |
| `pre-processed/` | **Photo dump** — raw images before any AI step (git ignores contents). |
| `post-processed/` | **Backgrounds for the video** — 16:9 images after `extend-backgrounds` (default input for the assembler). |
| `post-text-processed/` | Optional **captioned** stills after `add-text` (git ignores contents). |
| `prompts/background_master.txt` | **Master prompt** for Gemini: extend to widescreen + style hints (see file). |
| `fonts/` | Optional **`.ttf` / `.otf`** files; file stem becomes a `--font` key. |
| `output/` | Generated mix, titled frame, and final MP4. |

**Everything under `music/`**, **`pre-processed/`**, **`post-processed/`**, and **`post-text-processed/`** is ignored by git except each folder’s `.gitkeep`, so large assets are not committed by accident.

## Workflow: backgrounds (optional)

1. Drop source photos into **`pre-processed/`** (any aspect ratio).
2. Configure **`.env`** (see [Environment variables](#environment-variables-env) above): at minimum **`GEMINI_API_KEY`** from [Google AI Studio](https://aistudio.google.com/apikey).
3. **Extend every image** in the input folder (default **`pre-processed/`** → **`post-processed/`**). From the **repository root**, with your venv activated if you use one:

```bash
extend-backgrounds
```

That processes **all** supported files in `pre-processed/` (`.png`, `.jpg`, `.jpeg`, `.webp`), sorted by filename. If a matching PNG already exists in `post-processed/`, it is **skipped**—re-run everything and overwrite with:

```bash
extend-backgrounds --force
```

If the script is not on your `PATH`, use:

```bash
python3 -m music_assembler.extend_backgrounds
```

Custom folders (still “every image” in that folder):

```bash
extend-backgrounds --input-dir path/to/photos --output-dir path/to/out
```

The extender uses **`prompts/background_master.txt`** by default.

Each file is sent to the Gemini **image** model as **master prompt + source image**; the first image in the response is saved to **`post-processed/`** as PNG. **`image_config`** uses **`image_size`** (default **`2K`**) plus **`aspect_ratio`**; the saved file is then **resized to ~1600px wide** by default (preserving aspect ratio), since the API only offers discrete buckets (e.g. 3 Pro 16:9 is 1376×768 at 1K or 2752×1536 at 2K). Override **`GEMINI_IMAGE_MODEL`**, **`GEMINI_IMAGE_SIZE`**, **`GEMINI_OUTPUT_WIDTH`**, or the matching CLI flags as needed.

To process **only the first three** images (sorted by filename), run:

```bash
extend-first-three
```

**`assemble-music-video`** still encodes **1920×1080 (16:9)** MP4s and will **scale/pad** the still to fit.

Useful flags:

| Flag | Meaning |
|------|--------|
| `--input-dir` / `--output-dir` | Override folders (defaults: `pre-processed` / `post-processed`). |
| `--prompt-file` | Alternate prompt file. |
| `--model` | Gemini image model id (default: **`GEMINI_IMAGE_MODEL`** or `gemini-3-pro-image-preview`). |
| `--aspect-ratio` | Passed to image config (default **`16:9`**, or **`GEMINI_ASPECT_RATIO`**). |
| `--image-size` | `512`, `1K`, `2K`, or `4K` (default **`2K`**, or **`GEMINI_IMAGE_SIZE`**). |
| `--output-width` | Resize saved image to this width in pixels (default **`1600`**; **`0`** = native API size). |
| `--force` | Overwrite existing PNGs in the output folder. |
| `--limit N` | Only process the first N files (testing). |

Edit **`prompts/background_master.txt`** to tune the look. See the [Gemini image generation](https://ai.google.dev/gemini-api/docs/image-generation) docs for models and options.

## Workflow: bottom text on stills (optional)

Put **`.ttf` / `.otf`** files under **`fonts/`** (any subfolder). The default font is the **first** font file found (sorted by path); override with **`--font`** using the file **stem** as the key (e.g. `InriaSerif-Bold` → `--font InriaSerif-Bold`). If **`fonts/`** has no font files, the tools fall back to a **system** font (e.g. Arial on macOS).

**One image:**

```bash
add-bottom-text --text "Episode title" --input post-processed/image_004.png --output post-text-processed/image_004.png
```

**First three images** in **`post-processed/`** (by filename) → **`post-text-processed/`**:

```bash
add-text --text "Your line of text here"
```

Use **`--force`** to overwrite outputs. **Placement:** **`--h-align`**, **`--v-align`**, **`--margin`**. **Outline:** **`--stroke-width`** (default **0** = none; uses thin **8-direction** rings, not a solid square). **Weight:** **`--embolden`** (default **0** = lightest; try **1** or **2** for heavier type). **Size:** **`--font-size`** (default **96**). **Fonts:** Bundled **Inria Serif** faces live under **`fonts/Inria_Serif/`** (`InriaSerif-Light`, `InriaSerif-Regular`, `InriaSerif-Bold`, italics, etc.). Run **`add-text --list-fonts`** (or **`add-bottom-text --list-fonts`**) to print stems. The default when **`--font`** is omitted is **`fonts/Inria_Serif/InriaSerif-Light.ttf`** (stem **`InriaSerif-Light`**) when that file exists. Use **`--font InriaSerif-Regular`** for Regular. **Weight:** **`--font-weight`** (default **300**) applies when resolving without an exact **`--font`** stem. Edit **`DEFAULT_FONT_VARIANT`** / **`DEFAULT_FONT_WEIGHT`** at the top of **`music_assembler/add_text.py`** to change defaults. **`add-bottom-text`** supports the same options.

## Quick start (full video)

1. Add many `.mp3` files under `music/`.
2. Put **16:9** images in **`post-processed/`** (either export them yourself or run **`extend-backgrounds`** first).
3. Run:

```bash
assemble-music-video \
  --songs-dir music \
  --text "My mix title\nSubtitle or episode" \
  --output-dir output \
  --basename my_session
```

`--images-dir` defaults to **`post-processed`**; override if your backgrounds live elsewhere.

Outputs:

- `output/my_session_mix.mp3` — concatenated mix  
- `output/my_session_frame.png` — image with text  
- `output/my_session_video.mp4` — final video  

### Useful options

| Option | Description |
|--------|-------------|
| `--min-sec` / `--max-sec` | Target mix length in **seconds** (defaults: 4500 and 6300). |
| `--image filename.jpg` | Use a specific file inside `--images-dir` (default: **random** image). |
| `--seed N` | Fixed seed for **track order** and **random background** selection. |
| `--font arial` | Font key: built-ins include `arial`, `helvetica`, `georgia`, `times`, `sf_pro`, or a stem from `fonts/`. |
| `--font-size`, `--font-weight`, `--fill`, `--stroke`, `--stroke-width`, `--embolden` | Text appearance; optional **`--font-weight`** (e.g. **300** = Light) for files in **`fonts/`**; **`--stroke-width`** default **2**; **`--embolden`** default **0**. |
| `--h-align` / `--v-align` | `left` \| `center` \| `right` and `top` \| `center` \| `bottom`. |
| `--video-width` / `--video-height` | Encode size (default 1920×1080). |
| `--list-fonts` | Print available font keys (no FFmpeg needed). |

Example: **90–120 minute** mix:

```bash
assemble-music-video \
  --songs-dir music \
  --text "Evening set" \
  --min-sec 5400 \
  --max-sec 7200
```

Line breaks in text: use `\n` in the shell string, e.g. `--text "Line one\nLine two"`.

## Defaults in code

Default min/max durations and video size live in `music_assembler/config.py` (`DEFAULT_MIN_DURATION_SEC`, `DEFAULT_MAX_DURATION_SEC`, etc.). You can change them there or always override via the CLI.

## Python API

You can drive the same pipeline from code:

```python
from pathlib import Path
from music_assembler.config import (
    AssemblerConfig,
    AssemblerPaths,
    DurationBounds,
    TextOverlayStyle,
)
from music_assembler.pipeline import assemble

cfg = AssemblerConfig(
    paths=AssemblerPaths(
        songs_dir=Path("music"),
        images_dir=Path("post-processed"),
        output_dir=Path("output"),
        project_root=Path.cwd(),
    ),
    duration=DurationBounds(min_sec=75 * 60, max_sec=105 * 60),
    text=TextOverlayStyle(font_key="arial", font_size_px=72),
)
result = assemble(
    cfg,
    overlay_text="Title\nSubtitle",
    image_filename=None,
    output_basename="session",
)
print(result["video_mp4"])
```

## Module overview

| Module | Role |
|--------|------|
| `music_assembler/audio.py` | Discover MP3s, probe durations, random playlist, concat + trim. |
| `music_assembler/image_text.py` | Pillow rendering of text on the still. |
| `music_assembler/video.py` | FFmpeg: still image + audio → MP4. |
| `music_assembler/pipeline.py` | `assemble()` orchestrates the full run. |
| `music_assembler/cli.py` | `assemble-music-video` CLI. |
| `music_assembler/extend_backgrounds.py` | `extend-backgrounds` — Gemini `generate_content` with prompt + image; saves first image part. |
| `music_assembler/extend_first_three.py` | `extend-first-three` — first three images only (`--limit 3`). |
| `music_assembler/bottom_text_overlay.py` | Shared bottom-center overlay using `TextOverlayStyle` + `render_image_with_text`. |
| `music_assembler/add_bottom_text.py` | `add-bottom-text` — one input/output image. |
| `music_assembler/add_text.py` | `add-text` — first three in `post-processed/` → `post-text-processed/`; placement defaults in-module. |

## Troubleshooting

- **`ffmpeg` / `ffprobe` not found** — Install FFmpeg and ensure it is on your `PATH`.
- **No MP3 files** — Check `--songs-dir` and that files use the `.mp3` extension.
- **No images** — Supported extensions under the images directory include `.png`, `.jpg`, `.jpeg`, `.webp`. Default folder is **`post-processed/`**.
- **Font looks wrong** — Run `--list-fonts`, add a `.ttf` under `fonts/`, and pass `--font YourFontName` matching the file stem (or use a built-in key).
- **`extend-backgrounds` errors** — Confirm **`GEMINI_API_KEY`**, billing, and that **`GEMINI_IMAGE_MODEL`** is an image-capable Gemini id your project can call. If the response has text but no image, check safety blocks and shorten or soften the prompt in **`prompts/background_master.txt`**.
- **`ModuleNotFoundError: No module named 'music_assembler'`** — From the **repository root**, run **`python3 -m pip install .`** (not **`-e .`** on Python 3.14+; see [Install](#install)). **`python3 -c "import music_assembler"`** can still succeed when your shell’s cwd is the repo (current directory on `sys.path`) even if the package is not installed—test with **`cd /tmp && python3 -c "import music_assembler"`**; it should work only after **`pip install .`**.

## License

See the repository for license information (add a `LICENSE` file if you distribute this project).
