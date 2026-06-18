"""Upload a video to YouTube via the Data API v3 (OAuth installed-app flow).

On first run this opens a browser to authorize the channel and caches the OAuth
token (default ``youtube_token.json``) so later runs are non-interactive. Requires
a Google OAuth *client secret* JSON (Desktop app) — see ``--client-secret``.

Needs the optional extra:  pip install ".[youtube]"
"""

from __future__ import annotations

import errno
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

# Upload + manage thumbnails for the authorized channel.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# https://developers.google.com/youtube/v3/docs/videoCategories — 10 = Music.
DEFAULT_CATEGORY_ID = "10"
DEFAULT_PRIVACY = "private"
VALID_PRIVACY = ("private", "unlisted", "public")


def _require_google_libs():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:  # pragma: no cover - optional install
        raise RuntimeError(
            "YouTube upload needs extra packages. Install them with:\n"
            '    pip install ".[youtube]"\n'
            "(google-api-python-client, google-auth-oauthlib, google-auth-httplib2)."
        ) from e
    return (
        Request,
        Credentials,
        InstalledAppFlow,
        build,
        HttpError,
        MediaFileUpload,
    )


# YouTube custom-thumbnail limit is 2 MiB; stay a touch under to be safe.
MAX_THUMBNAIL_BYTES = 2_000_000


def _prepare_thumbnail(thumbnail_path: Path) -> tuple[Path, Path | None]:
    """Return a thumbnail path that satisfies YouTube's 2 MB limit.

    If the file is already small enough it's used as-is. Otherwise it's downscaled to fit
    1280x720 and re-encoded as JPEG (dropping quality until under the limit) into a temp
    file. Returns ``(path_to_upload, temp_path_to_delete_or_None)``.
    """
    try:
        if thumbnail_path.stat().st_size <= MAX_THUMBNAIL_BYTES:
            return thumbnail_path, None
    except OSError:
        return thumbnail_path, None

    import tempfile

    from PIL import Image

    fd, tmp_name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    tmp = Path(tmp_name)
    with Image.open(thumbnail_path) as im:
        img = im.convert("RGB")
    img.thumbnail((1280, 720))  # YouTube's recommended thumbnail size; preserves aspect
    quality = 90
    while True:
        img.save(tmp, format="JPEG", quality=quality, optimize=True)
        if tmp.stat().st_size <= MAX_THUMBNAIL_BYTES or quality <= 40:
            break
        quality -= 10
    return tmp, tmp


def find_client_secret(explicit: Path | None, project_root: Path) -> Path:
    """Use ``explicit`` if given, else auto-discover ``client_secret*.json`` in the root."""
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"Client secret file not found: {p}")
        return p
    candidates = sorted(project_root.glob("client_secret*.json"))
    if not candidates:
        raise FileNotFoundError(
            "No OAuth client secret found. Pass --client-secret /path/to/client_secret.json "
            "or place a client_secret*.json (Desktop app) in the project root."
        )
    return candidates[0].resolve()


def is_transient_upload_error(exc: BaseException) -> bool:
    """True for network blips and server-side errors worth retrying."""
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        retryable = {
            errno.ETIMEDOUT,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.EPIPE,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
        }
        if getattr(exc, "errno", None) in retryable:
            return True
    err_name = type(exc).__name__
    if err_name in {"TransportError", "HttpLib2Error"}:
        return True
    try:
        _Request, _Credentials, _Flow, _build, HttpError, _Media = _require_google_libs()
        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, "status", 0) or 0)
            return status in (408, 429, 500, 502, 503, 504)
    except RuntimeError:
        pass
    return False


def upload_video_with_retry(
    video_path: Path,
    *,
    max_attempts: int = 3,
    retry_delay_sec: float = 30.0,
    on_retry: Callable[[int, int, BaseException], None] | None = None,
    **upload_kwargs,
) -> dict:
    """Call :func:`upload_video`, retrying transient failures with linear backoff."""
    attempts = max(1, max_attempts)
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return upload_video(video_path, **upload_kwargs)
        except BaseException as e:
            last_error = e
            if attempt >= attempts or not is_transient_upload_error(e):
                raise
            if on_retry is not None:
                on_retry(attempt, attempts, e)
            time.sleep(retry_delay_sec * attempt)
    assert last_error is not None
    raise last_error


def get_credentials(client_secret: Path, token_path: Path, *, oauth_port: int = 8080):
    """Load cached OAuth creds, refreshing or running the browser flow as needed.

    ``oauth_port`` fixes the local redirect port. The redirect URI is sent WITHOUT a
    trailing slash (``http://localhost:<port>``) because Google's console won't accept a
    trailing slash for *Web application* clients — so register ``http://localhost:8080``
    (and ``http://127.0.0.1:8080``) exactly. Desktop app clients accept any localhost URI.
    """
    Request, Credentials, InstalledAppFlow, _build, _HttpError, _Media = _require_google_libs()

    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=oauth_port, redirect_uri_trailing_slash=False)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload_video(
    video_path: Path,
    *,
    title: str,
    description: str,
    client_secret: Path,
    token_path: Path,
    privacy: str = DEFAULT_PRIVACY,
    category_id: str = DEFAULT_CATEGORY_ID,
    tags: list[str] | None = None,
    made_for_kids: bool = False,
    thumbnail_path: Path | None = None,
    publish_at: str | None = None,
    oauth_port: int = 8080,
    on_progress=None,
) -> dict:
    """Upload ``video_path`` (resumable) and optionally set a custom thumbnail.

    Returns the inserted video resource (includes ``id``). ``on_progress`` is called
    with a 0.0–1.0 float as bytes upload.

    ``publish_at`` (RFC3339 UTC, e.g. ``2026-06-20T15:00:00Z``) schedules the video: it
    is uploaded private and YouTube makes it public at that time. When set, ``privacy``
    is forced to ``private`` (a YouTube requirement for scheduled publishing).
    """
    if privacy not in VALID_PRIVACY:
        raise ValueError(f"privacy must be one of {VALID_PRIVACY}, got {privacy!r}")
    if not video_path.is_file():
        raise FileNotFoundError(f"Video not found: {video_path}")

    _Request, _Credentials, _Flow, build, _HttpError, MediaFileUpload = _require_google_libs()
    creds = get_credentials(client_secret, token_path, oauth_port=oauth_port)
    youtube = build("youtube", "v3", credentials=creds)

    status: dict = {
        # Scheduled publishing requires the video to start private.
        "privacyStatus": "private" if publish_at else privacy,
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at:
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": status,
    }
    if tags:
        body["snippet"]["tags"] = tags

    media = MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and on_progress:
            on_progress(status.progress())
    if on_progress:
        on_progress(1.0)

    video_id = response.get("id")
    if thumbnail_path and video_id and thumbnail_path.is_file():
        send_path, tmp_path = _prepare_thumbnail(thumbnail_path)
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(send_path)),
            ).execute()
        except Exception as e:  # noqa: BLE001 - thumbnail is best-effort; video is already up
            # e.g. file >2MB, or custom thumbnails need a verified channel. Never fail the
            # (already uploaded) video because of the thumbnail.
            response["_thumbnail_warning"] = str(e)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    return response
