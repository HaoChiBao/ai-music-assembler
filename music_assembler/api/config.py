"""Assembly API settings from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv
from music_assembler.assemble_options import normalize_channel

load_dotenv(find_dotenv(usecwd=True))


@dataclass(frozen=True)
class ApiSettings:
    api_key: str | None
    dashboard_password: str | None
    gcp_project: str
    gcp_region: str
    assembly_job_name: str
    default_category: str
    configured_channels: tuple[str, ...]
    uploader_api_url: str | None
    uploader_api_key: str | None

    @classmethod
    def from_env(cls) -> ApiSettings:
        raw_channels = os.environ.get("ASSEMBLY_CHANNELS", "").strip()
        channels: list[str] = []
        for part in raw_channels.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                channels.append(normalize_channel(part) or part)
            except ValueError:
                channels.append(part.lower().replace(" ", "-"))
        return cls(
            api_key=os.environ.get("ASSEMBLY_API_KEY", "").strip() or None,
            dashboard_password=os.environ.get("ASSEMBLY_DASHBOARD_PASSWORD", "").strip() or None,
            gcp_project=os.environ.get("ASSEMBLY_GCP_PROJECT", "").strip()
            or os.environ.get("GCP_PROJECT", "").strip()
            or "youtube-uploader-499603",
            gcp_region=os.environ.get("ASSEMBLY_GCP_REGION", "").strip()
            or os.environ.get("GCP_REGION", "").strip()
            or "northamerica-northeast2",
            assembly_job_name=os.environ.get("ASSEMBLY_JOB_NAME", "music-assemble").strip(),
            default_category=os.environ.get("ASSEMBLY_CATEGORY", "korean").strip(),
            configured_channels=tuple(dict.fromkeys(channels)),
            uploader_api_url=os.environ.get("UPLOADER_API_URL", "").strip() or None,
            uploader_api_key=os.environ.get("UPLOADER_API_KEY", "").strip() or None,
        )

    @property
    def job_resource(self) -> str:
        return (
            f"projects/{self.gcp_project}/locations/{self.gcp_region}"
            f"/jobs/{self.assembly_job_name}"
        )
