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

- **`python3 -m music_assembler.make_short_music_video`** — **main command**: ~**1h15–1h30** mix from **`music/`**, random still from **`post-processed/`** → a timestamped **per-run folder** **`music-video/mv_*/`** holding **`frame.png`** (the still), **`mv_*_mix.mp3`**, **`mv_*_video.mp4`**, and a YouTube-ready **`mv_*_tracklist.txt`** (timestamp → song, for chapters). The video shows the **current song title in the bottom-left** (changes per track, equal left/bottom margins). Pass **`--thumbnail-text "..."`** to also render **`mv_*_thumbnail.png`** — the same still with that text in large letters drawn **behind the subject**, segmented at the **highest quality** (BiRefNet **`birefnet-general`** + alpha matting; downloads a ~1GB model on first use). No on-screen caption and no API keys needed.  
- **`python3 -m music_assembler.make_and_upload_music_video`** (`upload-music-video`) — runs the **main command** above, then **generates a YouTube title + description** with **OpenAI or Gemini** (prompt in **`prompts/youtube_metadata.txt`**, timestamped tracklist appended for chapters) and **uploads the video to YouTube**. Sets the rendered `--thumbnail-text` image as the custom thumbnail when provided. Needs the optional extra `pip install ".[youtube]"`, a Google OAuth **client secret** JSON (Desktop app) in the project root (auto-discovered) or via `--client-secret`, and a metadata key: **`OPENAI_API_KEY`** or **`GEMINI_API_KEY`** (provider auto-selected; force it with `--metadata-provider {auto,openai,gemini}`). Use **`--no-upload`** to build + preview the metadata without uploading, and **`--privacy`** (`private`/`unlisted`/`public`, default `private`).  
- `assemble-music-video` — same pipeline with full CLI flags (paths, duration, font, seed, etc.)  
- `extend-backgrounds` — pre-processed photos → widescreen post-processed backgrounds (Gemini **image** models; default **gemini-3-pro-image-preview**)  
- `extend-first-three` — same as `extend-backgrounds --limit 3` (handy for quick tests)  
- `add-bottom-text` — overlay caption text on **one** image; placement and outline are configurable (uses **`fonts/`** when present)  
- `add-text` — same text on the **first three** images in **`post-processed/`** → **`post-text-processed/`** (defaults: **96px**, **no outline**, **bottom center**)  
- `add-text-behind-subject` — separate an image's **subject** from its **background** into two layers, draw **large text behind the subject**, and composite the subject back on top → **`layer-text-image/`**. Uses a **random** image from **`post-processed/`** (or `--input`). Segmentation backends via **`--segmenter`**: **`rembg`** (default, local; needs the optional extra `pip install ".[segmentation]"`, downloads a model on first use) or **`gemini`** (Google Gemini segmentation via `GEMINI_API_KEY`, default model **`gemini-3-flash-preview`**; override with `--gemini-model` and target with `--subject-prompt`). **Edge quality:** default rembg model is **`isnet-general-use`** (finer hair than u2net); switch with **`--rembg-model`** (e.g. `birefnet-general` for the best hair, `u2net_human_seg` for people, `u2net` for speed). For natural, non-hand-cut edges the subject mask is eroded by **`--shrink`** px (removes the background halo) then feathered with **`--feather`** px (Gaussian blur, default **1.5**; try 2-4 for soft hair), and **`--alpha-matting`** adds matte-based soft edges. Use **`--text-opacity`** (e.g. 85) to let the background show through the letters for a more integrated look.  
- `assemble-from-r2` — sync MP3s + backgrounds from **Cloudflare R2**, assemble one video (with YouTube title/description by default), sync outputs back. See [Cloudflare R2](#cloudflare-r2-production-storage).
- `extend-from-r2` — pull a pre-processed photo from R2, extend with Gemini, upload to `post-processed/`, move source to `pre-processed/used/` on R2 (needs `GEMINI_API_KEY`).

Install optional extras as needed:

```bash
pip install ".[youtube]"      # YouTube upload (OAuth + Data API)
```

### Environment variables (`.env`)

1. Copy the example file:  
   `cp .env.example .env`
2. Edit **`.env`** (never commit it; it’s in `.gitignore`).

| Variable | Required | Used by |
|----------|----------|---------|
| `CLOUDFLARE_R2_BUCKET` | For **R2** workflows | Cloudflare R2 bucket name |
| `CLOUDFLARE_R2_ENDPOINT` | For **R2** workflows | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `CLOUDFLARE_R2_ACCESS_KEY_ID` | For **R2** workflows | R2 API token access key |
| `CLOUDFLARE_R2_SECRET_ACCESS_KEY` | For **R2** workflows | R2 API token secret |
| `ASSEMBLY_CATEGORY` | For **R2** assembly | Genre subfolder (e.g. `korean`) under `music/`, `pre-processed/`, `post-processed/` |
| `THUMBNAIL_TEXT` | No | Default `--thumbnail-text` for `assemble-from-r2` / Cloud Run job |
| `GEMINI_API_KEY` | For **`extend-backgrounds` only** | [Google AI Studio](https://aistudio.google.com/apikey) (same key works with the Gemini API) |
| `GEMINI_IMAGE_MODEL` | No | Image model id (default: `gemini-3-pro-image-preview`). Use another id from the [Gemini API model list](https://ai.google.dev/gemini-api/docs/models) if you prefer (e.g. `gemini-2.5-flash-image`). |
| `GEMINI_ASPECT_RATIO` | No | Output aspect ratio for the image config (default: `16:9`) |
| `GEMINI_IMAGE_SIZE` | No | API resolution bucket: `512`, `1K`, `2K`, or `4K` (default: **`2K`**). See [image generation](https://ai.google.dev/gemini-api/docs/image-generation) for pixel sizes per ratio. |
| `GEMINI_OUTPUT_WIDTH` | No | Resize saved PNG to this width in pixels (default: **`1600`**; `0` = keep native API dimensions). |

`python-dotenv` loads `.env` for **`extend-backgrounds`** and **`add-text-behind-subject --segmenter gemini`**.

The video commands (**`make_short_music_video`**, **`assemble-music-video`**) need **no API keys**—only FFmpeg and your images.

## Project layout

| Path | Purpose |
|------|---------|
| `music/` | **MP3 library** — use category subfolders (e.g. `music/korean/`). |
| `pre-processed/` | **Photo dump** — raw images before any AI step (git ignores contents). After `extend-backgrounds`, successful sources move to `pre-processed/used/`. |
| `post-processed/` | **Backgrounds for the video** — category subfolders (e.g. `post-processed/korean/`). |
| `post-text-processed/` | Optional **captioned** stills after `add-text` (git ignores contents). |
| `layer-text-image/` | Optional **text-behind-subject** composites after `add-text-behind-subject` (git ignores contents). |
| `prompts/background_master.txt` | **Master prompt** for Gemini: extend to widescreen + style hints (see file). |
| `fonts/` | Optional **`.ttf` / `.otf`** files; file stem becomes a `--font` key. |
| `output/` | Generated mix, final MP4, and tracklist when using **`assemble-music-video`** defaults. |
| `music-video/` | Default output for **`make_short_music_video`** — per category (e.g. `music-video/korean/mv_*/`) with `frame_*.png`, mix, video, tracklist, and optional thumbnail. Use **`--category korean`** when inputs live in category subfolders. |

**Everything under `music/`**, **`pre-processed/`**, **`post-processed/`**, **`post-text-processed/`**, and **`music-video/`** is ignored by git except each folder’s `.gitkeep`, so large assets are not committed by accident.

## Cloudflare R2 (production storage)

For hosted assembly, assets live in a **Cloudflare R2** bucket with the same category layout as local folders. Full tree and upload examples: **[docs/r2-bucket-layout.md](docs/r2-bucket-layout.md)**.

```
s3://{bucket}/
├── music/{category}/
├── pre-processed/{category}/ + used/
├── post-processed/{category}/ + used/
└── music-video/{category}/mv_*/
```

**Initialize** empty category folders (after filling `CLOUDFLARE_R2_*` in `.env`):

```bash
./scripts/r2-init-layout.sh          # uses ASSEMBLY_CATEGORY from .env
./scripts/r2-init-layout.sh korean   # or pass category explicitly
```

**Assemble one video from R2** (download → encode → upload):

```bash
pip install ".[r2]"
assemble-from-r2                     # uses .env CLOUDFLARE_R2_* + ASSEMBLY_CATEGORY
assemble-from-r2 --category korean --keep-work-dir
assemble-from-r2 --download-only     # sync inputs only
```

**Background extension on R2:**

```powershell
pip install ".[r2]"
extend-from-r2                     # one image per run (default)
extend-from-r2 --all               # process every pending photo
extend-from-r2 --limit 3           # batch of three
```

Cloud Run job entrypoint: `scripts/assemble-job.sh` — see [deploy/cloud-run-job.md](deploy/cloud-run-job.md).

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

After each successful extend, the source photo is moved to **`pre-processed/used/`** so it is not picked up again. Use **`--no-move-used`** to leave sources in place.

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

## Quick start: generate a video

1. Add many `.mp3` files under **`music/korean/`** (or another category subfolder).
2. Put **16:9** images in **`post-processed/korean/`** (export them yourself or run **`extend-backgrounds`** first).
3. From the **repository root**, run:

```bash
python3 -m music_assembler.make_short_music_video
```

No arguments needed. Each run creates its own folder **`music-video/mv_<timestamp>/`** (e.g. **`music-video/mv_20260412_143022/`**) containing **`frame.png`** (the still used for the video), **`mv_20260412_143022_mix.mp3`**, **`mv_20260412_143022_video.mp4`**, and **`mv_20260412_143022_tracklist.txt`**. The video shows the **current song title in the bottom-left** (changing per track, with equal left/bottom margins); the tracklist file maps each **timestamp → song** for YouTube chapters. Mix length is about **1h15–1h30** by design; the tool picks a **random** background and avoids playing the same logical track title back-to-back. Optional: **`--title-font-size`** to resize the song title, and **`--thumbnail-text "Late Night"`** to also write **`mv_*_thumbnail.png`** — the same still with that text drawn **behind the subject**.

## Generate a video and upload it to YouTube

**`upload-music-video`** runs the same pipeline, then auto-writes a YouTube **title + description** (Gemini) and **uploads** the video.

One-time setup:

1. Install the upload extra: `pip install ".[youtube]"`
2. In [Google Cloud Console](https://console.cloud.google.com/): create a project, enable the **YouTube Data API v3**, configure the OAuth consent screen, and create an **OAuth client ID** of type **Desktop app**. Download its JSON and drop it in the project root (any `client_secret*.json` is auto-discovered) — it is **gitignored**, never commit it.
3. Make sure **`OPENAI_API_KEY`** is in `.env` (used to write the title/description; **OpenAI is the default** provider). Switch with `--metadata-provider {openai,gemini,auto}` (Gemini uses `GEMINI_API_KEY`). The metadata is generated **early** — right after the tracklist, before the slow encode — so a bad key or API error fails fast instead of after rendering the video.

Then:

```bash
# Build + generate metadata + upload (defaults to PRIVATE)
python3 -m music_assembler.make_and_upload_music_video --thumbnail-text "Late Night"

# Preview the generated title/description without uploading
python3 -m music_assembler.make_and_upload_music_video --no-upload

# Upload as unlisted with tags
python3 -m music_assembler.make_and_upload_music_video --privacy unlisted --tags "lofi,chill,study"
```

- On the **first** upload a browser opens to authorize your channel; the token is cached to **`youtube_token.json`** (gitignored) for subsequent non-interactive runs.
- The **title/description prompt** lives at **`prompts/youtube_metadata.txt`** — edit it to change tone/format. The timestamped tracklist is appended automatically so **YouTube chapters** work. Metadata is written by **OpenAI** (default model `gpt-4o-mini`, override with `OPENAI_TEXT_MODEL`) or **Gemini** (`gemini-2.5-flash`, override with `GEMINI_TEXT_MODEL`).
- When `--thumbnail-text` is given, the rendered thumbnail is set as the video's **custom thumbnail** (requires a verified channel; otherwise it's skipped with a warning).
- Other flags: `--privacy {private,unlisted,public}` (default `private`), `--category-id` (default `10` = Music), `--made-for-kids`, `--client-secret PATH`, `--token-file PATH`, `--metadata-prompt PATH`.

## Batch: generate many videos, then schedule uploads

For producing a backlog, split the work into two steps: **build N videos in parallel**, then **mass-schedule** their uploads.

### 1. Generate N videos in parallel (`generate-music-videos`)

```bash
# Build 5 videos at once, each with its own progress bar + thumbnail
generate-music-videos -n 5 --thumbnail-text "Late Night" --workers 3
```

- Each video gets a **distinct** random background (so backgrounds aren't reused within the batch), its own `music-video/<base>/` folder, and a Gemini-generated **title/description** saved as `<base>_title.txt` / `<base>_description.txt`.
- A live **per-video progress bar** shows each build's status; failures are isolated (one bad video won't stop the rest).
- Every built video is appended as a **`pending`** entry to a registry: **`music-video/video_registry.txt`** (JSON-lines; this is the list of generated video ids).
- Generated titles are checked against the **used-titles log** (and against each other in the batch) so they're never reused.
- Flags: `-n/--count` (required), `--workers` (default `min(count, 3)`), `--thumbnail-text`, `--title-font-size`, `--registry PATH`, `--metadata-prompt PATH`, `--used-titles-file PATH`, `--no-metadata`.

### 2. Mass-schedule the uploads (`schedule-music-videos`)

```bash
# Preview the schedule without uploading
schedule-music-videos --start "2026-06-20 09:00" --interval-hours 24 --dry-run

# Upload all pending videos, scheduled to publish 1/day starting that date
schedule-music-videos --start "2026-06-20 09:00" --interval-hours 24
```

- Reads the **`pending`** entries from the registry and uploads each to YouTube.
- By default it **schedules** them: each video is uploaded private and set to go **public** at a staggered time (`--start` + `--interval-hours` × index). Use **`--no-schedule`** to upload immediately at `--privacy`.
- On success, each entry is flipped to **`uploaded`** in the registry with its **YouTube id**, watch URL, and scheduled publish time, and its title is appended to the used-titles log.
- Flags: `--start "'YYYY-MM-DD HH:MM'"` (local time; default tomorrow 09:00), `--interval-hours` (default `24`), `--limit N`, `--no-schedule`, `--privacy`, `--category-id`, `--tags`, `--made-for-kids`, `--client-secret PATH`, `--token-file PATH`, `--registry PATH`, `--dry-run`.

### Full CLI (`assemble-music-video`)

For custom folders, mix length, fixed seed, and song-title fonts, use **`assemble-music-video`**:

```bash
assemble-music-video \
  --songs-dir music \
  --output-dir output \
  --basename my_session
```

`--images-dir` defaults to **`post-processed`**; override if your backgrounds live elsewhere.

Outputs land in a per-run folder **`output/my_session/`**:

- `output/my_session/frame.png` — the still used for the video  
- `output/my_session/my_session_mix.mp3` — concatenated mix  
- `output/my_session/my_session_video.mp4` — final video (bottom-left song titles)  
- `output/my_session/my_session_tracklist.txt` — timestamp → song list for YouTube chapters  
- `output/my_session/my_session_thumbnail.png` — only when `--thumbnail-text` is given (text drawn behind the subject)  

### Useful options (`assemble-music-video`)

| Option | Description |
|--------|-------------|
| `--min-sec` / `--max-sec` | Target mix length in **seconds** (defaults: 4500 and 6300). |
| `--image filename.jpg` | Use a specific file inside `--images-dir` (default: **random** image). |
| `--seed N` | Fixed seed for **track order** and **random background** selection. |
| `--font arial` | Song-title font key: built-ins include `arial`, `helvetica`, `georgia`, `times`, `sf_pro`, or a stem from `fonts/`. Non-Latin titles (e.g. Korean) auto-fall back to a Unicode system font. |
| `--font-size`, `--font-weight`, `--fill` | Song-title appearance; **`--font-size`** default **46**, **`--font-weight`** default **400**, **`--fill`** `R,G,B[,A]` (default white). |
| `--thumbnail-text "Late Night"` | Also render `<base>_thumbnail.png`: this text in large letters **behind the subject** of the background still. |
| `--video-width` / `--video-height` | Encode size (default 1920×1080). |
| `--list-fonts` | Print available font keys (no FFmpeg needed). |

Example: **90–120 minute** mix:

```bash
assemble-music-video \
  --songs-dir music \
  --min-sec 5400 \
  --max-sec 7200
```

## Defaults in code

**`make_short_music_video`** uses its own targets (**~1h15–1h30**, output **`music-video/`**, bottom-left song-title styling) in `music_assembler/make_short_music_video.py`. For **`assemble-music-video`**, default min/max durations and video size live in `music_assembler/config.py` (`DEFAULT_MIN_DURATION_SEC`, `DEFAULT_MAX_DURATION_SEC`, etc.); change them there or override via the CLI. The title margin/size/color defaults live at the top of `music_assembler/music_video.py`.

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
    # TextOverlayStyle now styles the bottom-left per-song title.
    text=TextOverlayStyle(font_key="arial", font_size_px=46, font_weight=400),
)
result = assemble(
    cfg,
    image_filename=None,
    output_basename="session",
    # Optional: also render a thumbnail with this text drawn *behind* the subject.
    thumbnail_background_text="Late Night",
)
print(result["output_dir"])     # output/session/
print(result["frame_png"])      # output/session/frame.png
print(result["video_mp4"])
print(result["tracklist_txt"])
print(result["thumbnail_png"])  # None unless thumbnail_background_text was given
```

## Module overview

| Module | Role |
|--------|------|
| `music_assembler/audio.py` | Discover MP3s, probe durations, random playlist, concat + trim, per-song display titles + track segments. |
| `music_assembler/image_text.py` | Pillow rendering of text on the still. |
| `music_assembler/music_video.py` | FFmpeg: still + per-song bottom-left titles → MP4; writes the tracklist. |
| `music_assembler/video.py` | FFmpeg helper: plain still image + audio → MP4. |
| `music_assembler/pipeline.py` | `assemble()` orchestrates the full run. |
| `music_assembler/cli.py` | `assemble-music-video` CLI. |
| `music_assembler/make_short_music_video.py` | **`python3 -m music_assembler.make_short_music_video`** — opinionated defaults → **`music-video/`**. |
| `music_assembler/assemble_from_r2.py` | **`assemble-from-r2`** — R2 sync → assemble → sync back. |
| `music_assembler/extend_from_r2.py` | **`extend-from-r2`** — R2 pre-processed → Gemini extend → post-processed on R2. |
| `music_assembler/r2_storage.py` | boto3 helpers for Cloudflare R2 prefix sync. |
| `music_assembler/make_and_upload_music_video.py` | **`upload-music-video`** — build the video, generate metadata, upload to YouTube. |
| `music_assembler/make_music_videos.py` | **`generate-music-videos`** — build N videos in parallel (per-video progress bars) → registry. |
| `music_assembler/schedule_music_videos.py` | **`schedule-music-videos`** — mass-upload pending registry videos on a publish schedule, mark uploaded. |
| `music_assembler/video_registry.py` | JSON-lines registry of generated videos + upload status (`music-video/video_registry.txt`). |
| `music_assembler/progress_bars.py` | Thread-safe multi-line progress bars for parallel jobs. |
| `music_assembler/youtube_metadata.py` | OpenAI/Gemini YouTube title/description (+ appended tracklist chapters), unique-title tracking. |
| `music_assembler/youtube_upload.py` | OAuth + resumable YouTube Data API v3 upload (custom thumbnail, scheduled `publishAt`). |
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
