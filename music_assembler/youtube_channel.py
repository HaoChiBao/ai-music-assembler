"""List videos on the authorized YouTube channel via the Data API v3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from music_assembler.youtube_upload import get_credentials


@dataclass
class YouTubeVideoInfo:
    """Summary of one video on the authenticated user's channel."""

    video_id: str
    title: str
    privacy_status: str
    publish_at: str | None  # RFC3339 UTC from the API; set when scheduled to go public
    url: str

    @property
    def is_scheduled(self) -> bool:
        """True when the video is private and set to publish in the future."""
        if self.privacy_status != "private" or not self.publish_at:
            return False
        return _parse_api_datetime(self.publish_at) > datetime.now(timezone.utc)

    def publish_at_local(self) -> datetime | None:
        if not self.publish_at:
            return None
        return _parse_api_datetime(self.publish_at).astimezone()


def _parse_api_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _build_youtube(client_secret: Path, token_path: Path, *, oauth_port: int = 8080):
    from music_assembler.youtube_upload import _require_google_libs

    _Request, _Credentials, _Flow, build, _HttpError, _Media = _require_google_libs()
    creds = get_credentials(client_secret, token_path, oauth_port=oauth_port)
    return build("youtube", "v3", credentials=creds)


def _uploads_playlist_id(youtube) -> str:
    response = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = response.get("items") or []
    if not items:
        raise RuntimeError("No YouTube channel found for the authorized account.")
    playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    if not playlist_id:
        raise RuntimeError("Could not resolve the channel uploads playlist.")
    return playlist_id


def _iter_upload_video_ids(youtube, playlist_id: str) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    while True:
        request = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        )
        response = request.execute()
        for item in response.get("items") or []:
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                ids.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids


def list_channel_videos(
    client_secret: Path,
    token_path: Path,
    *,
    scheduled_only: bool = False,
    oauth_port: int = 8080,
) -> list[YouTubeVideoInfo]:
    """Return metadata for every video on the OAuth-authorized channel.

    When ``scheduled_only`` is True, only videos that are **private** with a future
    ``publishAt`` (YouTube scheduled publish) are returned, sorted soonest-first.
    """
    youtube = _build_youtube(client_secret, token_path, oauth_port=oauth_port)
    playlist_id = _uploads_playlist_id(youtube)
    video_ids = _iter_upload_video_ids(youtube, playlist_id)

    videos: list[YouTubeVideoInfo] = []
    for offset in range(0, len(video_ids), 50):
        batch = video_ids[offset : offset + 50]
        response = (
            youtube.videos()
            .list(part="snippet,status", id=",".join(batch))
            .execute()
        )
        for item in response.get("items") or []:
            video_id = item.get("id", "")
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            publish_at = status.get("publishAt") or None
            videos.append(
                YouTubeVideoInfo(
                    video_id=video_id,
                    title=snippet.get("title") or video_id,
                    privacy_status=status.get("privacyStatus") or "",
                    publish_at=publish_at,
                    url=f"https://youtu.be/{video_id}" if video_id else "",
                )
            )

    if scheduled_only:
        videos = [v for v in videos if v.is_scheduled]
        videos.sort(key=lambda v: _parse_api_datetime(v.publish_at or ""))
    else:
        videos.sort(key=lambda v: v.title.casefold())

    return videos
