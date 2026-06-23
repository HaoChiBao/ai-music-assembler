"""CLI: pull pre-processed photos from R2, extend with Gemini, upload to post-processed, retire sources.

Flow per image:

1. Pick the next image in ``pre-processed/{category}/`` on R2 (skips ``used/`` and existing outputs).
2. Download it to a local work dir.
3. Call Gemini (same as ``extend-backgrounds``) → PNG in ``post-processed/``.
4. Upload the PNG to ``post-processed/{category}/`` on R2.
5. Move the original on R2 to ``pre-processed/{category}/used/``.

By default processes **one** image per run (handy for cron). Use ``--all`` or ``--limit N`` for batches.

Requires ``pip install ".[r2]"``, ``GEMINI_API_KEY``, and ``CLOUDFLARE_R2_*`` in ``.env``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from dotenv import find_dotenv, load_dotenv

from music_assembler import __version__
from music_assembler.extend_backgrounds import (
    DEFAULT_ASPECT_RATIO,
    DEFAULT_IMAGE_SIZE,
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_WIDTH,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_BACKOFF,
    DEFAULT_WORKERS,
    ContentBlockedError,
    _load_prompt,
    _log,
    _move_to_used,
    extend_one_with_retry,
)
from music_assembler.extend_backgrounds import IMAGE_EXTS
from music_assembler.r2_storage import (
    list_object_keys,
    move_object,
    r2_client,
    r2_config_from_env,
    upload_file,
)


def _is_image_name(name: str) -> bool:
    return Path(name).suffix in IMAGE_EXTS


def _post_processed_stems(client, bucket: str, images_prefix: str) -> set[str]:
    """Stems of PNGs already in post-processed/ (one list call, no per-image HEAD)."""
    stems: set[str] = set()
    for key in list_object_keys(
        client,
        bucket,
        images_prefix,
        exclude_relative_prefixes=("used/", "in-flight/"),
    ):
        name = Path(key).name
        if name.startswith(".") or not name.lower().endswith(".png"):
            continue
        if "/" in key[len(images_prefix) :]:
            continue
        stems.add(Path(name).stem)
    return stems


def pending_r2_sources(
    client,
    cfg,
    *,
    force: bool,
) -> list[str]:
    """Full R2 keys for pre-processed images that still need extending."""
    keys = list_object_keys(
        client,
        cfg.bucket,
        cfg.pre_processed_prefix,
        exclude_relative_prefixes=("used/",),
    )
    existing = set() if force else _post_processed_stems(client, cfg.bucket, cfg.images_prefix)
    pending: list[str] = []
    for key in keys:
        name = Path(key).name
        if name.startswith(".") or not _is_image_name(name):
            continue
        if not force and Path(name).stem in existing:
            continue
        pending.append(key)
    return pending


def _resolve_work_dir(arg: Path | None) -> tuple[Path, bool]:
    if arg is not None:
        return arg.resolve(), False
    env = os.environ.get("WORK_DIR", "").strip()
    if env:
        return Path(env).resolve(), False
    return Path(tempfile.mkdtemp(prefix="r2-extend-")).resolve(), True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extend-from-r2",
        description=(
            "Download pre-processed photos from Cloudflare R2, extend them to widescreen "
            "backgrounds with Gemini, upload PNGs to post-processed/, and move sources to "
            "pre-processed/used/ on R2."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--category",
        default=None,
        help="Genre subfolder (default: ASSEMBLY_CATEGORY from .env).",
    )
    p.add_argument("--work-dir", type=Path, default=None, help="Local scratch directory.")
    p.add_argument("--keep-work-dir", action="store_true", help="Keep scratch dir when done.")
    p.add_argument(
        "--all",
        action="store_true",
        help="Process every pending image (default: one image per run).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N images (overrides default of 1 unless --all).",
    )
    p.add_argument("--force", action="store_true", help="Re-extend even if post-processed PNG exists on R2.")
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Download pending source(s) from R2 and exit without calling Gemini.",
    )
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="Extend locally but skip uploading PNGs / moving sources on R2.",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        default=Path("prompts/background_master.txt"),
        help="Master prompt text file.",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
        help=f"Gemini image model (default: {DEFAULT_MODEL}).",
    )
    p.add_argument(
        "--aspect-ratio",
        default=os.environ.get("GEMINI_ASPECT_RATIO", DEFAULT_ASPECT_RATIO),
    )
    p.add_argument(
        "--image-size",
        default=os.environ.get("GEMINI_IMAGE_SIZE", DEFAULT_IMAGE_SIZE),
        choices=("512", "1K", "2K", "4K"),
    )
    p.add_argument(
        "--output-width",
        type=int,
        default=int(os.environ.get("GEMINI_OUTPUT_WIDTH", str(DEFAULT_OUTPUT_WIDTH))),
        help=f"Resize output PNG to this width (0 = native). Default {DEFAULT_OUTPUT_WIDTH}.",
    )
    p.add_argument("--workers", "-j", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF)
    return p


def count_pending_r2_sources(
    client,
    cfg,
    *,
    force: bool = False,
) -> int:
    return len(pending_r2_sources(client, cfg, force=force))


def run_extend_from_r2(
    *,
    category: str | None = None,
    limit: int | None = 1,
    process_all: bool = False,
    force: bool = False,
    work_dir: Path | None = None,
    keep_work_dir: bool = False,
    download_only: bool = False,
    no_upload: bool = False,
    prompt_file: Path | None = None,
    model: str | None = None,
    aspect_ratio: str | None = None,
    image_size: str | None = None,
    output_width: int | None = None,
    workers: int | None = None,
    retries: int | None = None,
    retry_backoff: float | None = None,
    source_keys: list[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Extend pre-processed R2 photos to post-processed PNGs (library entry point)."""
    def _cancelled() -> bool:
        return should_cancel is not None and should_cancel()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key and not download_only:
        raise RuntimeError("Missing GEMINI_API_KEY in environment.")

    cfg_r2 = r2_config_from_env(category=category)
    client = r2_client(cfg_r2)

    if source_keys is not None:
        pending_keys = list(source_keys)
    else:
        pending_keys = pending_r2_sources(client, cfg_r2, force=force)
    if not pending_keys:
        if on_progress:
            on_progress(100, "No pending images")
        return {"ok": 0, "failed": 0, "pending": 0, "failures": [], "cancelled": False}

    if _cancelled():
        if on_progress:
            on_progress(0, "Cancelled before start")
        return {"ok": 0, "failed": 0, "pending": len(pending_keys), "failures": [], "cancelled": True}

    if source_keys is not None:
        to_process = list(source_keys)
        total = len(to_process)
    else:
        if process_all:
            batch_limit = len(pending_keys)
        elif limit is not None:
            batch_limit = max(0, limit)
        else:
            batch_limit = 1
        to_process = pending_keys[:batch_limit]
        total = len(to_process)
    if on_progress:
        on_progress(0, f"Preparing {total} image(s)…")

    work_dir, is_temp = _resolve_work_dir(work_dir)
    input_dir = work_dir / "pre-processed"
    output_dir = work_dir / "post-processed"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    local_sources: list[tuple[str, Path]] = []
    for i, key in enumerate(to_process):
        if _cancelled():
            if on_progress:
                on_progress(0, "Cancelled during download")
            return {
                "ok": 0,
                "failed": 0,
                "pending": len(pending_keys),
                "failures": [],
                "cancelled": True,
            }
        dest = input_dir / Path(key).name
        client.download_file(cfg_r2.bucket, key, str(dest))
        local_sources.append((key, dest))
        if on_progress:
            on_progress(
                (i + 1) / max(total * 4, 1) * 100,
                f"Downloaded {Path(key).name} ({i + 1}/{total})",
            )

    if download_only:
        return {"ok": 0, "failed": 0, "pending": len(pending_keys), "failures": [], "work_dir": str(work_dir)}

    try:
        from google import genai
    except ImportError as exc:
        raise RuntimeError('Install dependencies: pip install google-genai ".[r2]"') from exc

    prompt_path = prompt_file or Path("prompts/background_master.txt")
    prompt = _load_prompt(prompt_path.resolve())
    out_w = (
        output_width
        if output_width is not None
        else int(os.environ.get("GEMINI_OUTPUT_WIDTH", str(DEFAULT_OUTPUT_WIDTH)))
    )
    out_w = out_w if out_w > 0 else None
    gemini = genai.Client(api_key=api_key)

    tasks: list[tuple[str, Path, Path]] = []
    for r2_key, src in local_sources:
        dest = output_dir / f"{src.stem}.png"
        tasks.append((r2_key, src, dest))

    worker_count = workers if workers is not None else int(os.environ.get("EXTEND_WORKERS", "1"))
    worker_count = max(1, min(worker_count, len(tasks)))
    model_name = model or os.environ.get("GEMINI_IMAGE_MODEL", DEFAULT_MODEL)
    aspect = aspect_ratio or os.environ.get("GEMINI_ASPECT_RATIO", DEFAULT_ASPECT_RATIO)
    img_size = image_size or os.environ.get("GEMINI_IMAGE_SIZE", DEFAULT_IMAGE_SIZE)
    retry_count = retries if retries is not None else DEFAULT_RETRIES
    backoff = retry_backoff if retry_backoff is not None else DEFAULT_RETRY_BACKOFF

    ok = 0
    failures: list[tuple[str, str]] = []
    uploaded_pngs: list[Path] = []
    moved_r2_keys: list[str] = []
    completed = 0
    progress_lock = threading.Lock()

    def _report(pct: float, stage: str) -> None:
        if on_progress is None:
            return
        with progress_lock:
            on_progress(pct, stage)

    def _process(r2_key: str, src: Path, dest: Path) -> tuple[str, bool, str | None]:
        if _cancelled():
            return (r2_key, False, "cancelled")
        _log(f"extend: {src.name} -> {dest.name}")
        try:
            extend_one_with_retry(
                retries=retry_count,
                retry_backoff=backoff,
                client=gemini,
                model=model_name,
                prompt=prompt,
                image_path=src,
                out_path=dest,
                aspect_ratio=aspect,
                image_size=img_size,
                output_width=out_w,
            )
            moved = _move_to_used(src, input_dir)
            if moved is not None:
                _log(f"ok: {src.name} (local used/{moved.name})")
            else:
                _log(f"ok: {src.name}")
            return (r2_key, True, None)
        except ContentBlockedError as e:
            return (r2_key, False, f"blocked: {e}")
        except Exception as e:
            return (r2_key, False, str(e))

    if worker_count == 1:
        for r2_key, src, dest in tasks:
            if _cancelled():
                break
            idx = completed + 1
            _report(
                25 + ((idx - 1) / max(total, 1)) * 50,
                f"Calling Gemini for {src.name} ({idx}/{total})…",
            )
            r2_key, succeeded, err = _process(r2_key, src, dest)
            completed += 1
            if err == "cancelled":
                failures.append((r2_key, err))
                continue
            if succeeded:
                ok += 1
                uploaded_pngs.append(output_dir / f"{Path(r2_key).stem}.png")
                moved_r2_keys.append(r2_key)
            else:
                _log(f"error: {Path(r2_key).name}: {err}", err=True)
                failures.append((r2_key, err or "unknown"))
            _report(
                25 + (completed / max(total, 1)) * 50,
                f"Extended {Path(r2_key).name} ({completed}/{total})",
            )
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(_process, k, s, d): (k, s) for k, s, d in tasks}
            for fut in as_completed(futures):
                if _cancelled():
                    for pending_fut in futures:
                        pending_fut.cancel()
                    break
                r2_key, src = futures[fut]
                r2_key, succeeded, err = fut.result()
                completed += 1
                if err == "cancelled":
                    failures.append((r2_key, err))
                    continue
                if succeeded:
                    ok += 1
                    uploaded_pngs.append(output_dir / f"{Path(r2_key).stem}.png")
                    moved_r2_keys.append(r2_key)
                else:
                    _log(f"error: {Path(r2_key).name}: {err}", err=True)
                    failures.append((r2_key, err or "unknown"))
                _report(
                    25 + (completed / max(total, 1)) * 50,
                    f"Extended {Path(r2_key).name} ({completed}/{total})",
                )

    if _cancelled():
        if is_temp and not keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        return {
            "ok": ok,
            "failed": len(failures),
            "pending": len(pending_keys),
            "failures": [{"key": k, "error": e} for k, e in failures],
            "cancelled": True,
        }

    if not no_upload and uploaded_pngs:
        if on_progress:
            on_progress(80, f"Uploading {len(uploaded_pngs)} PNG(s) to R2…")
        for i, png in enumerate(uploaded_pngs):
            key = f"{cfg_r2.images_prefix}{png.name}"
            upload_file(client, cfg_r2.bucket, key, png)
            if on_progress:
                on_progress(
                    80 + (i + 1) / max(len(uploaded_pngs), 1) * 15,
                    f"Uploaded {png.name} ({i + 1}/{len(uploaded_pngs)})",
                )

        for r2_key in moved_r2_keys:
            name = Path(r2_key).name
            used_key = f"{cfg_r2.used_pre_processed_prefix}{name}"
            move_object(client, cfg_r2.bucket, r2_key, used_key)

    if is_temp and not keep_work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "ok": ok,
        "failed": len(failures),
        "pending": len(pending_keys),
        "failures": [{"key": k, "error": e} for k, e in failures],
        "cancelled": False,
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))
    args = build_parser().parse_args(argv)

    if args.all:
        limit = None
        process_all = True
    elif args.limit is not None:
        limit = args.limit
        process_all = False
    else:
        limit = 1
        process_all = False

    def _print_progress(pct: float, msg: str) -> None:
        print(f" [{pct:.0f}%] {msg}")

    try:
        result = run_extend_from_r2(
            category=args.category,
            limit=limit,
            process_all=process_all,
            force=args.force,
            work_dir=args.work_dir,
            keep_work_dir=args.keep_work_dir,
            download_only=args.download_only,
            no_upload=args.no_upload,
            prompt_file=args.prompt_file,
            model=args.model,
            aspect_ratio=args.aspect_ratio,
            image_size=args.image_size,
            output_width=args.output_width,
            workers=args.workers,
            retries=args.retries,
            retry_backoff=args.retry_backoff,
            on_progress=_print_progress if not args.download_only else None,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    pending = result.get("pending", 0)
    ok = result.get("ok", 0)
    failures = result.get("failures", [])
    if ok == 0 and not failures and pending == 0:
        cfg_r2 = r2_config_from_env(category=args.category)
        print(f"No pending images in s3://{cfg_r2.bucket}/{cfg_r2.pre_processed_prefix}")
        return 0
    if args.download_only:
        print("==> Download-only; files saved locally")
        return 0

    print(f"==> {ok} image(s) extended (of {pending} pending)")
    print(f"Done. extended {ok}, failed {len(failures)}.")
    for item in failures:
        print(f"  - {Path(item['key']).name}: {item['error'].splitlines()[0]}", file=sys.stderr)
    return 1 if (ok == 0 and failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
