"""CLI: sync MP3s + backgrounds from Cloudflare R2, assemble one music video, sync outputs back.

Mirrors ``scripts/assemble-job.sh`` but uses boto3 instead of the AWS CLI, so it runs on
Windows and anywhere Python is available.

Bucket layout: see ``docs/r2-bucket-layout.md``.

Required ``.env`` keys: ``CLOUDFLARE_R2_*`` and ``ASSEMBLY_CATEGORY`` (or folder flags).
Optional: ``THUMBNAIL_TEXT``, ``ASSEMBLY_DURATION_MIN`` / ``ASSEMBLY_VARIANCE_MIN``,
``OPENAI_API_KEY`` / ``GEMINI_API_KEY`` for YouTube metadata.
Optional: ``--queue-youtube`` / ``ASSEMBLY_QUEUE_YOUTUBE`` to register on the youtube-uploader
pending queue after R2 upload (needs ``UPLOADER_API_URL`` + ``UPLOADER_API_KEY``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.assemble_options import (
    add_duration_arguments,
    add_r2_folder_arguments,
    duration_bounds_from_env,
    resolve_duration_bounds,
    resolve_folder_args,
    resolve_r2_assembly_prefixes,
    unique_output_basename,
)
from music_assembler.bottom_text_overlay import resolve_font_key
from music_assembler.config import AssemblerConfig, AssemblerPaths, TextOverlayStyle
from music_assembler.ffmpeg_util import FFmpegNotFoundError, find_ffmpeg, find_ffprobe
from music_assembler.pipeline import assemble
from music_assembler.r2_storage import (
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    claim_background_on_r2,
    count_files_with_suffixes,
    has_files_with_suffixes,
    in_flight_key,
    r2_client,
    r2_config_from_env,
    release_background_claim,
    retire_claimed_background_on_r2,
    retire_used_background_on_r2,
    sync_dir_to_prefix,
    sync_prefix_to_dir,
    verify_background_retired_on_r2,
)
from music_assembler.youtube_metadata import (
    DEFAULT_PROMPT_FILE,
    DEFAULT_USED_TITLES_FILE,
    load_used_titles,
    record_used_title,
)

try:
    from music_assembler.api.uploader_client import (
        register_youtube_upload,
        resolve_queue_youtube,
        r2_object_uri,
        uploader_credentials_from_env,
    )
except ImportError:  # pragma: no cover
    register_youtube_upload = None  # type: ignore[misc, assignment]
    resolve_queue_youtube = None  # type: ignore[misc, assignment]
    r2_object_uri = None  # type: ignore[misc, assignment]
    uploader_credentials_from_env = None  # type: ignore[misc, assignment]

DEFAULT_TITLE_FONT_SIZE = 46
DEFAULT_TITLE_FONT_WEIGHT = 400


def _print_preflight(duration) -> None:
    avg_min = (duration.min_sec + duration.max_sec) / 2 / 60.0
    print(
        f"==> Expect ffmpeg encode to take a while (~{avg_min:.0f} min target video; "
        "often 20-60+ min on a laptop). Do not interrupt unless stuck.",
        file=sys.stderr,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assemble-from-r2",
        description=(
            "Download MP3s and post-processed stills from Cloudflare R2, build a music-mix "
            "MP4 with configurable duration, then upload the run folder and any used "
            "backgrounds back to R2."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    add_r2_folder_arguments(p)
    add_duration_arguments(p)
    p.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Local scratch directory (default: WORK_DIR env or a temp dir under the system temp folder).",
    )
    p.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Do not delete the work directory when finished (useful for debugging).",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Sync inputs from R2 and exit without assembling or uploading.",
    )
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Assemble locally but skip uploading outputs to R2.",
    )
    p.add_argument(
        "--no-thumbnail",
        action="store_true",
        help="Skip thumbnail generation (faster).",
    )
    p.add_argument(
        "--title-font-size",
        type=int,
        default=DEFAULT_TITLE_FONT_SIZE,
        help=f"Bottom-left song-title font size in px (default: {DEFAULT_TITLE_FONT_SIZE}).",
    )
    p.add_argument(
        "--thumbnail-text",
        default=None,
        metavar="TEXT",
        help=(
            "Render a thumbnail with this text behind the subject. "
            "Default: THUMBNAIL_TEXT from .env."
        ),
    )
    p.add_argument(
        "--thumbnail-bottom-text",
        default=None,
        metavar="TEXT",
        help="Caption on top of the thumbnail, centered along the bottom.",
    )
    p.add_argument(
        "--metadata-prompt",
        type=Path,
        default=DEFAULT_PROMPT_FILE,
        help=f"Prompt file for YouTube title/description (default: {DEFAULT_PROMPT_FILE}).",
    )
    p.add_argument(
        "--metadata-provider",
        choices=("auto", "openai", "gemini"),
        default=None,
        help="Model for title/description (default: YOUTUBE_METADATA_PROVIDER or auto).",
    )
    p.add_argument(
        "--used-titles-file",
        type=Path,
        default=DEFAULT_USED_TITLES_FILE,
        help=f"Avoid reusing past YouTube titles (default: {DEFAULT_USED_TITLES_FILE}).",
    )
    p.add_argument(
        "--no-metadata",
        action="store_true",
        help="Skip YouTube title/description generation.",
    )
    p.add_argument(
        "--queue-youtube",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "After R2 upload, register the video on the youtube-uploader pending queue "
            "(default: on; set ASSEMBLY_QUEUE_YOUTUBE=false or --no-queue-youtube to skip). "
            "Requires channel, metadata, and UPLOADER_API_URL + UPLOADER_API_KEY."
        ),
    )
    return p


def _format_duration_range(bounds) -> str:
    return f"{bounds.min_sec / 60:.0f}-{bounds.max_sec / 60:.0f} min"


def _read_text_file(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _maybe_queue_youtube_upload(
    *,
    enabled: bool,
    bucket: str,
    output_prefix: str,
    channel: str | None,
    basename: str,
    result: dict,
    no_upload: bool,
) -> dict[str, Any] | None:
    """Register the finished run with youtube-uploader; returns API response or None."""
    if not enabled:
        return None
    if register_youtube_upload is None:
        print(
            "warning: --queue-youtube requested but uploader client is unavailable",
            file=sys.stderr,
        )
        return None
    if no_upload:
        print(
            "warning: --queue-youtube skipped because --no-upload was set (video not on R2)",
            file=sys.stderr,
        )
        return None
    if not channel:
        print(
            "warning: --queue-youtube skipped because no YouTube channel was set",
            file=sys.stderr,
        )
        return None

    api_url, api_key = uploader_credentials_from_env()
    if not api_url or not api_key:
        print(
            "warning: --queue-youtube skipped — set UPLOADER_API_URL and UPLOADER_API_KEY",
            file=sys.stderr,
        )
        return None

    meta = result.get("youtube_metadata")
    title = meta.title if meta else _read_text_file(result.get("title_txt"))
    description = meta.description if meta else _read_text_file(result.get("description_txt"))
    if not title:
        print(
            "warning: --queue-youtube skipped — no title (use metadata generation, not --no-metadata)",
            file=sys.stderr,
        )
        return None

    video_key = f"{output_prefix}{basename}/{basename}_video.mp4"
    video_uri = r2_object_uri(bucket, video_key)
    thumbnail_uri = ""
    thumb_path = result.get("thumbnail_png")
    if thumb_path is not None:
        thumbnail_uri = r2_object_uri(bucket, f"{output_prefix}{basename}/{Path(thumb_path).name}")

    print(f"==> Register YouTube upload queue ({channel})")
    print(f"    video: {video_uri}")
    upload_privacy = os.environ.get("ASSEMBLY_UPLOAD_PRIVACY", "").strip() or None
    publish_at = os.environ.get("ASSEMBLY_PUBLISH_AT", "").strip() or None
    upload_at = os.environ.get("ASSEMBLY_UPLOAD_AT", "").strip() or None
    # Late-assembly guard: if the planned go-live/upload time already passed while
    # encoding, bump to a few minutes after finish so Cloud Scheduler can still arm.
    try:
        from music_assembler.api.assembly_schedule import effective_schedule_at
    except ImportError:  # pragma: no cover
        effective_schedule_at = None  # type: ignore[assignment,misc]
    if effective_schedule_at is not None:
        adjusted_publish = effective_schedule_at(publish_at)
        adjusted_upload = effective_schedule_at(upload_at or publish_at)
        if publish_at and adjusted_publish and adjusted_publish != publish_at:
            print(
                f"    late schedule: publish_at {publish_at} → {adjusted_publish} "
                "(assembly finished after planned time)"
            )
        if (upload_at or publish_at) and adjusted_upload and adjusted_upload != (upload_at or publish_at):
            print(
                f"    late schedule: upload_at {upload_at or publish_at} → {adjusted_upload}"
            )
        publish_at = adjusted_publish
        upload_at = adjusted_upload
    upload_tags_raw = os.environ.get("ASSEMBLY_UPLOAD_TAGS", "").strip()
    upload_tags = [t.strip() for t in upload_tags_raw.split(",") if t.strip()] if upload_tags_raw else None
    category_id = os.environ.get("ASSEMBLY_UPLOAD_CATEGORY_ID", "").strip() or None
    made_for_kids_raw = os.environ.get("ASSEMBLY_UPLOAD_MADE_FOR_KIDS", "").strip().lower()
    made_for_kids = made_for_kids_raw in ("1", "true", "yes", "on") if made_for_kids_raw else None
    try:
        response = register_youtube_upload(
            api_url=api_url,
            api_key=api_key,
            channel=channel,
            title=title,
            description=description,
            video_uri=video_uri,
            thumbnail_uri=thumbnail_uri,
            job_id=basename,
            tags=upload_tags,
            privacy=upload_privacy,
            publish_at=publish_at,
            upload_at=upload_at,
            category_id=category_id,
            made_for_kids=made_for_kids,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"warning: YouTube queue register failed: {exc}", file=sys.stderr)
        return None

    job_id = response.get("job_id") or basename
    status = response.get("status") or "pending"
    print(f"    queued job_id={job_id} status={status}")
    return response


def _resolve_work_dir(arg: Path | None) -> tuple[Path, bool]:
    """Return (path, is_temporary)."""
    if arg is not None:
        return arg.resolve(), False
    env = os.environ.get("WORK_DIR", "").strip()
    if env:
        return Path(env).resolve(), False
    return Path(tempfile.mkdtemp(prefix="r2-assemble-")).resolve(), True


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    args = build_parser().parse_args(argv)

    try:
        find_ffmpeg()
        find_ffprobe()
    except FFmpegNotFoundError as e:
        print(e, file=sys.stderr)
        return 2

    category, music_folder, images_folder, output_folder, channel = resolve_folder_args(args)
    prefixes = resolve_r2_assembly_prefixes(
        category=category,
        music_folder=music_folder,
        images_folder=images_folder,
        output_folder=output_folder,
        channel=channel,
    )
    duration_sec = args.duration
    variance_sec = args.variance
    if duration_sec is None or variance_sec is None:
        env_duration_sec, env_variance_sec = duration_bounds_from_env()
        if duration_sec is None:
            duration_sec = env_duration_sec
        if variance_sec is None:
            variance_sec = env_variance_sec
    duration = resolve_duration_bounds(
        duration_sec=duration_sec,
        variance_sec=variance_sec,
        min_sec=args.min_sec,
        max_sec=args.max_sec,
    )

    cfg_r2 = r2_config_from_env(category=category or prefixes.music_folder)
    client = r2_client(cfg_r2)

    execution_id = os.environ.get("ASSEMBLY_EXECUTION_ID", "").strip()
    progress_write = None
    if execution_id:
        from music_assembler.job_progress import write_progress_json

        print(f"==> Job progress tracking: {execution_id}", flush=True)

        def progress_write(pct: float, stage: str, *, status: str = "running") -> None:
            write_progress_json(
                client,
                cfg_r2.bucket,
                execution_id,
                pct=pct,
                stage=stage,
                category=prefixes.images_folder,
                status=status,
            )
            print(f"\r  [{pct:5.1f}%] {stage}", end="", flush=True, file=sys.stderr)

        progress_write(2, "Worker started; syncing from R2…")
    elif os.environ.get("ASSEMBLY_EXECUTION_ID"):
        print(
            "warning: ASSEMBLY_EXECUTION_ID is set but empty — progress will not sync to R2",
            file=sys.stderr,
        )

    work_dir, is_temp = _resolve_work_dir(args.work_dir)
    music_dir = work_dir / "music"
    images_dir = work_dir / "post-processed"
    output_dir = work_dir / "music-video"

    print(
        f"==> Sync inputs (music={prefixes.music_folder}, "
        f"backgrounds={prefixes.images_folder}, output={prefixes.output_folder}"
        f"{', channel=' + prefixes.channel if prefixes.channel else ''})"
    )
    print(f"    target duration: {_format_duration_range(duration)}")
    print(f"    s3://{cfg_r2.bucket}/{prefixes.music_prefix} -> {music_dir}")
    if progress_write:
        progress_write(3, "Downloading music from R2…")
    music_count = sync_prefix_to_dir(
        client, cfg_r2.bucket, prefixes.music_prefix, music_dir
    )
    print(f"    downloaded {music_count} object(s)")

    claimed_background: str | None = None
    image_filename: str | None = None

    if execution_id:
        if progress_write:
            progress_write(8, "Claiming exclusive background…")
        print(f"==> Claim exclusive background for job {execution_id}")
        claimed_background = claim_background_on_r2(
            client,
            cfg_r2.bucket,
            images_prefix=prefixes.images_prefix,
            execution_id=execution_id,
        )
        if claimed_background is None:
            msg = (
                "No backgrounds available — all images are in use (in-flight) or already in used/. "
                "Wait for parallel jobs to finish or add more post-processed images."
            )
            print(f"error: {msg}", file=sys.stderr)
            try:
                from music_assembler.job_progress import write_progress_json

                write_progress_json(
                    client,
                    cfg_r2.bucket,
                    execution_id,
                    pct=0,
                    stage=msg,
                    category=prefixes.images_folder,
                    status="failed",
                )
            except ImportError:
                pass
            return 1
        images_dir.mkdir(parents=True, exist_ok=True)
        claim_key = in_flight_key(
            prefixes.images_prefix, execution_id, claimed_background
        )
        local_bg = images_dir / claimed_background
        print(f"    claimed {claimed_background} -> {claim_key}")
        client.download_file(cfg_r2.bucket, claim_key, str(local_bg))
        image_filename = claimed_background
        image_count = 1
        if progress_write:
            progress_write(10, f"Background claimed: {claimed_background}")
    else:
        print(f"    s3://{cfg_r2.bucket}/{prefixes.images_prefix} -> {images_dir}")
        image_count = sync_prefix_to_dir(
            client,
            cfg_r2.bucket,
            prefixes.images_prefix,
            images_dir,
            exclude_relative_prefixes=("used/", "in-flight/"),
        )
        print(f"    downloaded {image_count} object(s)")

    if not has_files_with_suffixes(music_dir, AUDIO_EXTENSIONS):
        if execution_id and claimed_background:
            release_background_claim(
                client,
                cfg_r2.bucket,
                images_prefix=prefixes.images_prefix,
                execution_id=execution_id,
                filename=claimed_background,
            )
        print(
            f"error: no MP3s in s3://{cfg_r2.bucket}/{prefixes.music_prefix}",
            file=sys.stderr,
        )
        return 1
    if not has_files_with_suffixes(images_dir, IMAGE_EXTENSIONS):
        if execution_id and claimed_background:
            release_background_claim(
                client,
                cfg_r2.bucket,
                images_prefix=prefixes.images_prefix,
                execution_id=execution_id,
                filename=claimed_background,
            )
        print(
            f"error: no backgrounds in s3://{cfg_r2.bucket}/{prefixes.images_prefix}",
            file=sys.stderr,
        )
        return 1

    mp3s = count_files_with_suffixes(music_dir, AUDIO_EXTENSIONS)
    bgs = count_files_with_suffixes(images_dir, IMAGE_EXTENSIONS)
    print(f"==> Ready: {mp3s} MP3(s), {bgs} background image(s) in {work_dir}")

    if args.download_only:
        print("==> Download-only; skipping assembly")
        return 0

    project_root = Path.cwd().resolve()
    font_key = resolve_font_key(project_root, None, weight=DEFAULT_TITLE_FONT_WEIGHT)
    basename = unique_output_basename(execution_id)

    thumbnail_text = None if args.no_thumbnail else args.thumbnail_text
    if thumbnail_text is None and not args.no_thumbnail:
        thumbnail_text = os.environ.get("THUMBNAIL_TEXT", "").strip() or None
    if thumbnail_text:
        thumbnail_text = thumbnail_text.replace("\\n", "\n")
    thumbnail_bottom_text = (
        args.thumbnail_bottom_text.replace("\\n", "\n") if args.thumbnail_bottom_text else None
    )

    _print_preflight(duration)

    progress_cb = None
    if execution_id:
        from music_assembler.job_progress import write_meta_json

        write_meta_json(
            client,
            cfg_r2.bucket,
            execution_id,
            category=prefixes.images_folder,
            claimed_background=claimed_background,
            channel=prefixes.channel,
        )

        def progress_cb(pct: float, msg: str) -> None:
            if progress_write:
                progress_write(pct, msg)

        progress_cb(12, "Sync complete; assembling…")

    want_metadata = not args.no_metadata
    used_titles_path = (project_root / args.used_titles_file).resolve()
    used_titles = load_used_titles(used_titles_path) if want_metadata else None

    assembler_cfg = AssemblerConfig(
        paths=AssemblerPaths(
            songs_dir=music_dir.resolve(),
            images_dir=images_dir.resolve(),
            output_dir=output_dir.resolve(),
            project_root=project_root,
        ),
        duration=duration,
        text=TextOverlayStyle(
            font_key=font_key,
            font_size_px=args.title_font_size,
            font_weight=DEFAULT_TITLE_FONT_WEIGHT,
            fill_color=(255, 255, 255, 235),
        ),
        video_width=1920,
        video_height=1080,
        seed=None,
    )

    print(f"==> Assemble one video in {work_dir}")
    try:
        result = assemble(
            assembler_cfg,
            output_basename=basename,
            image_filename=image_filename,
            progress=progress_cb is None,
            on_progress=progress_cb,
            thumbnail_background_text=thumbnail_text,
            thumbnail_bottom_text=thumbnail_bottom_text,
            generate_metadata=want_metadata,
            metadata_provider=args.metadata_provider,
            metadata_prompt_path=args.metadata_prompt.resolve(),
            metadata_used_titles=used_titles,
            move_used_image=True,
        )
    except (RuntimeError, OSError, ValueError) as e:
        if execution_id:
            from music_assembler.job_progress import write_progress_json

            if claimed_background:
                release_background_claim(
                    client,
                    cfg_r2.bucket,
                    images_prefix=prefixes.images_prefix,
                    execution_id=execution_id,
                    filename=claimed_background,
                )
            write_progress_json(
                client,
                cfg_r2.bucket,
                execution_id,
                pct=0,
                stage=str(e),
                category=prefixes.images_folder,
                status="failed",
            )
        print(f"error: assembly failed: {e}", file=sys.stderr)
        return 1

    print(f"Wrote folder: {result['output_dir']}")
    for k in ("frame_png", "audio_mp3", "video_mp4", "tracklist_txt"):
        print(f"  {k}: {result[k]}")
    if result.get("thumbnail_png"):
        print(f"  thumbnail_png: {result['thumbnail_png']}")
    if result.get("title_txt"):
        print(f"  title_txt: {result['title_txt']}")
        print(f"  description_txt: {result['description_txt']}")
    meta = result.get("youtube_metadata")
    if meta:
        print("\n--- YouTube metadata ---")
        print(f"Title: {meta.title}")
        print("Description:")
        print(meta.description)
        print("------------------------\n")
        record_used_title(meta.title, used_titles_path)
    if result.get("used_image"):
        print(f"  moved used image -> {result['used_image']}")
    dur = result["final_audio_duration_sec"]
    print(f"  audio duration: {dur / 60:.1f} min ({dur:.0f} s)")

    # Retire background on R2 after successful encode (dashboard jobs use in-flight claim).
    if execution_id and claimed_background:
        name = claimed_background
        used_image = result.get("used_image")
        print(
            f"==> Retire used background on R2 (in-flight → used/, delete source): "
            f"{in_flight_key(prefixes.images_prefix, execution_id, name)} -> "
            f"{prefixes.used_images_prefix}{name}"
        )
        if progress_write:
            progress_write(95, f"Retiring background {name} to used/")
        retired = retire_claimed_background_on_r2(
            client,
            cfg_r2.bucket,
            images_prefix=prefixes.images_prefix,
            used_images_prefix=prefixes.used_images_prefix,
            execution_id=execution_id,
            filename=name,
            local_used_path=Path(used_image) if used_image else None,
        )
        check = verify_background_retired_on_r2(
            client,
            cfg_r2.bucket,
            images_prefix=prefixes.images_prefix,
            used_images_prefix=prefixes.used_images_prefix,
            filename=name,
            execution_id=execution_id,
        )
        if retired and check["in_used"] and not check["in_pool"] and not check["in_flight"]:
            print(f"    retired {name} → used/ (verified: not in pool or in-flight)")
        elif retired:
            print(f"    retired {name} (verify: {check})")
        else:
            print(
                f"warning: could not retire {name} on R2 (verify: {check})",
                file=sys.stderr,
            )
    elif not args.no_upload:
        used_image = result.get("used_image")
        if used_image is not None:
            name = Path(used_image).name
            print(
                f"==> Retire used background on R2 (copy → used/, delete original): "
                f"{prefixes.images_prefix}{name} -> {prefixes.used_images_prefix}{name}"
            )
            retired = retire_used_background_on_r2(
                client,
                cfg_r2.bucket,
                images_prefix=prefixes.images_prefix,
                used_images_prefix=prefixes.used_images_prefix,
                filename=name,
                local_used_path=Path(used_image),
            )
            check = verify_background_retired_on_r2(
                client,
                cfg_r2.bucket,
                images_prefix=prefixes.images_prefix,
                used_images_prefix=prefixes.used_images_prefix,
                filename=name,
            )
            if retired:
                print(f"    retired {name} → used/ (verify: {check})")
            else:
                print(f"warning: could not retire {name} on R2", file=sys.stderr)

    if not args.no_upload:
        print(f"==> Sync outputs to s3://{cfg_r2.bucket}/{prefixes.output_prefix}")
        if progress_write:
            progress_write(96, "Uploading outputs to R2…")
        uploaded = sync_dir_to_prefix(client, cfg_r2.bucket, output_dir, prefixes.output_prefix)
        print(f"    uploaded {uploaded} object(s)")

    queue_youtube = resolve_queue_youtube(args.queue_youtube) if resolve_queue_youtube else False
    queue_result = _maybe_queue_youtube_upload(
        enabled=queue_youtube,
        bucket=cfg_r2.bucket,
        output_prefix=prefixes.output_prefix,
        channel=prefixes.channel,
        basename=basename,
        result=result,
        no_upload=args.no_upload,
    )
    if queue_result and progress_write:
        progress_write(99, f"YouTube queue: {queue_result.get('job_id', basename)}")

    if execution_id:
        from music_assembler.job_progress import write_progress_json

        write_progress_json(
            client,
            cfg_r2.bucket,
            execution_id,
            pct=100,
            stage="Complete",
            category=prefixes.images_folder,
            status="succeeded",
            extra={
                "output_folder": str(result["output_dir"]),
                "video_id": basename,
                "channel": prefixes.channel,
            },
        )
        if progress_write:
            print("\r  [100.0%] Complete", end="", flush=True, file=sys.stderr)

    print("==> Done")

    if is_temp and not args.keep_work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    elif args.keep_work_dir or not is_temp:
        print(f"    work dir kept at: {work_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
