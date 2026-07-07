"""Per-channel assembly schedules — R2 config, slot evaluation, idempotency ledger."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from music_assembler.api import gcp_jobs
from music_assembler.api.config import ApiSettings
from music_assembler.api.progress_store import patch_meta_gcp_execution_id, write_meta_json, write_progress_json
from music_assembler.api.r2_catalog import category_inventory
from music_assembler.extend_from_r2 import count_pending_r2_sources
from music_assembler.r2_storage import r2_config_from_env

SCHEDULES_KEY = "schedules/schedules.json"
SCHEDULE_RUNS_PREFIX = "schedules/runs/"
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DAY_ABBR = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_UPLOAD_OFFSET_MINUTES = 60
DEFAULT_ASSEMBLE_AT = "11:00"
VALID_UPLOAD_PRIVACY = ("private", "unlisted", "public")
SCHEDULE_DAY_ABBR = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


@dataclass
class DaySlot:
    enabled: bool = False
    assemble_at: str = DEFAULT_ASSEMBLE_AT
    upload_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": self.enabled, "assemble_at": self.assemble_at}
        if self.upload_at:
            out["upload_at"] = self.upload_at
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DaySlot:
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled")),
            assemble_at=_normalize_time(str(data.get("assemble_at") or DEFAULT_ASSEMBLE_AT)),
            upload_at=_normalize_time_optional(data.get("upload_at")),
        )


@dataclass
class ChannelSchedule:
    channel: str
    enabled: bool = True
    timezone: str = "America/New_York"
    category: str | None = None
    images_folder: str | None = None
    duration_min: int = 90
    variance_min: int = 15
    thumbnail_text: str | None = None
    queue_youtube: bool = True
    upload_privacy: str = "private"
    upload_schedule_publish: bool = True
    upload_tags: str = ""
    upload_category_id: str = "10"
    upload_made_for_kids: bool = False
    default_assemble_at: str = DEFAULT_ASSEMBLE_AT
    default_upload_at: str | None = None
    min_backgrounds: int = 1
    auto_extend: bool = True
    days: list[DaySlot] = field(default_factory=lambda: [DaySlot() for _ in range(7)])

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "category": self.category,
            "images_folder": self.images_folder,
            "duration_min": self.duration_min,
            "variance_min": self.variance_min,
            "thumbnail_text": self.thumbnail_text,
            "queue_youtube": self.queue_youtube,
            "upload_privacy": self.upload_privacy,
            "upload_schedule_publish": self.upload_schedule_publish,
            "upload_tags": self.upload_tags,
            "upload_category_id": self.upload_category_id,
            "upload_made_for_kids": self.upload_made_for_kids,
            "default_assemble_at": self.default_assemble_at,
            "default_upload_at": self.default_upload_at,
            "min_backgrounds": self.min_backgrounds,
            "auto_extend": self.auto_extend,
            "days": [d.to_dict() for d in self.days],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelSchedule:
        channel = str(data.get("channel") or "").strip()
        if not channel:
            raise ValueError("channel is required")
        raw_days = data.get("days") or []
        days: list[DaySlot] = []
        for i in range(7):
            item = raw_days[i] if i < len(raw_days) and isinstance(raw_days[i], dict) else {}
            days.append(DaySlot.from_dict(item))
        return cls(
            channel=channel,
            enabled=bool(data.get("enabled", True)),
            timezone=str(data.get("timezone") or "America/New_York").strip(),
            category=(str(data["category"]).strip() if data.get("category") else None),
            images_folder=(str(data["images_folder"]).strip() if data.get("images_folder") else None),
            duration_min=int(data.get("duration_min") or 90),
            variance_min=int(data.get("variance_min") or 15),
            thumbnail_text=(str(data["thumbnail_text"]).strip() if data.get("thumbnail_text") else None),
            queue_youtube=bool(data.get("queue_youtube", True)),
            upload_privacy=(
                str(data.get("upload_privacy") or "private").strip().lower()
                if str(data.get("upload_privacy") or "private").strip().lower() in VALID_UPLOAD_PRIVACY
                else "private"
            ),
            upload_schedule_publish=bool(data.get("upload_schedule_publish", True)),
            upload_tags=str(data.get("upload_tags") or "").strip(),
            upload_category_id=str(data.get("upload_category_id") or "10").strip() or "10",
            upload_made_for_kids=bool(data.get("upload_made_for_kids", False)),
            default_assemble_at=_normalize_time(str(data.get("default_assemble_at") or DEFAULT_ASSEMBLE_AT)),
            default_upload_at=_normalize_time_optional(data.get("default_upload_at")),
            min_backgrounds=max(1, int(data.get("min_backgrounds") or 1)),
            auto_extend=bool(data.get("auto_extend", True)),
            days=days,
        )


def resolved_upload_at(day: DaySlot, schedule: ChannelSchedule) -> str:
    assemble_at = day.assemble_at or schedule.default_assemble_at
    if day.upload_at:
        return day.upload_at
    if schedule.default_upload_at:
        return schedule.default_upload_at
    return upload_time_after_assemble(assemble_at)


def _normalize_time(value: str) -> str:
    m = _TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid time {value!r}; use HH:MM")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time {value!r}")
    return f"{hour:02d}:{minute:02d}"


def _normalize_time_optional(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _normalize_time(str(value))


def upload_time_after_assemble(
    assemble_at: str,
    offset_minutes: int = DEFAULT_UPLOAD_OFFSET_MINUTES,
) -> str:
    """Return HH:MM upload time ``offset_minutes`` after ``assemble_at`` (wraps at midnight)."""
    hour, minute = map(int, _normalize_time(assemble_at).split(":"))
    total = (hour * 60 + minute + offset_minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _normalize_upload_privacy(value: Any) -> str:
    raw = str(value or "private").strip().lower()
    if raw not in VALID_UPLOAD_PRIVACY:
        raise ValueError(f"upload_privacy must be one of {VALID_UPLOAD_PRIVACY}")
    return raw


def slot_publish_at_utc(slot: dict[str, Any], schedule: ChannelSchedule) -> str | None:
    """RFC3339 UTC publish time from slot upload_at, or None for immediate upload."""
    if not schedule.queue_youtube or not schedule.upload_schedule_publish:
        return None
    upload_at = slot.get("upload_at")
    if not upload_at:
        return None
    local_date = date.fromisoformat(str(slot["local_date"]))
    tz = ZoneInfo(schedule.timezone)
    dt = datetime.combine(local_date, _parse_local_time(str(upload_at)), tzinfo=tz)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_schedule_runs(
    client,
    bucket: str,
    *,
    channel: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List idempotency ledger entries under ``schedules/runs/``."""
    rows: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=SCHEDULE_RUNS_PREFIX):
        for obj in page.get("Contents") or []:
            key = obj.get("Key") or ""
            if not key.endswith(".json"):
                continue
            try:
                resp = client.get_object(Bucket=bucket, Key=key)
                entry = json.loads(resp["Body"].read().decode("utf-8"))
            except client.exceptions.ClientError:
                continue
            if channel and entry.get("channel") != channel:
                continue
            entry["ledger_key"] = key
            rows.append(entry)
    rows.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    return rows[: max(limit, 0)]


def delete_schedule_run(client, bucket: str, slot_key: str) -> bool:
    """Remove a ledger entry so the slot can fire again."""
    try:
        client.delete_object(Bucket=bucket, Key=_ledger_key(slot_key))
        return True
    except client.exceptions.ClientError:
        return False


def _ledger_key(slot_key: str) -> str:
    safe = slot_key.replace("/", "_").replace(":", "-")
    return f"{SCHEDULE_RUNS_PREFIX}{safe}.json"


def slot_key(channel: str, local_date: date, dow: int, assemble_at: str) -> str:
    return f"{channel}:{local_date.isoformat()}:{dow}:{assemble_at}"


def load_schedules_document(client, bucket: str) -> dict[str, Any]:
    try:
        resp = client.get_object(Bucket=bucket, Key=SCHEDULES_KEY)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return {"version": 1, "schedules": []}
        raise


def save_schedules_document(client, bucket: str, doc: dict[str, Any]) -> None:
    doc["version"] = int(doc.get("version") or 1)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    client.put_object(
        Bucket=bucket,
        Key=SCHEDULES_KEY,
        Body=json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def list_schedules(client, bucket: str) -> list[ChannelSchedule]:
    doc = load_schedules_document(client, bucket)
    rows = []
    for item in doc.get("schedules") or []:
        if isinstance(item, dict) and item.get("channel"):
            rows.append(ChannelSchedule.from_dict(item))
    rows.sort(key=lambda s: s.channel.lower())
    return rows


def get_schedule(client, bucket: str, channel: str) -> ChannelSchedule | None:
    channel = channel.strip()
    for sched in list_schedules(client, bucket):
        if sched.channel == channel:
            return sched
    return None


def upsert_schedule(client, bucket: str, schedule: ChannelSchedule) -> ChannelSchedule:
    doc = load_schedules_document(client, bucket)
    schedules = [ChannelSchedule.from_dict(s) for s in doc.get("schedules") or [] if isinstance(s, dict)]
    replaced = False
    for i, existing in enumerate(schedules):
        if existing.channel == schedule.channel:
            schedules[i] = schedule
            replaced = True
            break
    if not replaced:
        schedules.append(schedule)
    schedules.sort(key=lambda s: s.channel.lower())
    doc["schedules"] = [s.to_dict() for s in schedules]
    save_schedules_document(client, bucket, doc)
    return schedule


def delete_schedule(client, bucket: str, channel: str) -> bool:
    doc = load_schedules_document(client, bucket)
    before = doc.get("schedules") or []
    after = [s for s in before if isinstance(s, dict) and s.get("channel") != channel]
    if len(after) == len(before):
        return False
    doc["schedules"] = after
    save_schedules_document(client, bucket, doc)
    return True


def apply_default_times(schedule: ChannelSchedule, *, assemble_at: str | None = None, upload_at: str | None = None) -> ChannelSchedule:
    if assemble_at is not None:
        schedule.default_assemble_at = _normalize_time(assemble_at)
        for day in schedule.days:
            if day.enabled:
                day.assemble_at = schedule.default_assemble_at
    if upload_at is not None:
        schedule.default_upload_at = _normalize_time_optional(upload_at)
        for day in schedule.days:
            if day.enabled:
                day.upload_at = schedule.default_upload_at
    elif assemble_at is not None:
        computed = upload_time_after_assemble(schedule.default_assemble_at)
        schedule.default_upload_at = computed
        for day in schedule.days:
            if day.enabled:
                day.upload_at = computed
    return schedule


def _parse_local_time(value: str) -> time:
    hour, minute = map(int, _normalize_time(value).split(":"))
    return time(hour=hour, minute=minute)


def _slot_matches(now_local: datetime, assemble_at: str, window_minutes: int) -> bool:
    target = datetime.combine(now_local.date(), _parse_local_time(assemble_at), tzinfo=now_local.tzinfo)
    end = target + timedelta(minutes=window_minutes)
    return target <= now_local < end


def due_slots(
    schedule: ChannelSchedule,
    *,
    now_utc: datetime | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> list[dict[str, Any]]:
    if not schedule.enabled:
        return []
    tz = ZoneInfo(schedule.timezone)
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    dow = now_local.weekday()
    # Python weekday: Mon=0; our days[0]=Sunday
    day_index = (dow + 1) % 7
    day = schedule.days[day_index]
    if not day.enabled:
        return []
    assemble_at = day.assemble_at or schedule.default_assemble_at
    if not _slot_matches(now_local, assemble_at, window_minutes):
        return []
    key = slot_key(schedule.channel, now_local.date(), day_index, assemble_at)
    return [
        {
            "slot_key": key,
            "channel": schedule.channel,
            "local_date": now_local.date().isoformat(),
            "day_index": day_index,
            "day_name": DAY_NAMES[day_index],
            "assemble_at": assemble_at,
            "upload_at": resolved_upload_at(day, schedule),
            "timezone": schedule.timezone,
        }
    ]


def read_ledger(client, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        resp = client.get_object(Bucket=bucket, Key=_ledger_key(key))
        return json.loads(resp["Body"].read().decode("utf-8"))
    except client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def write_ledger(client, bucket: str, key: str, payload: dict[str, Any]) -> None:
    body = {**payload, "slot_key": key, "updated_at": datetime.now(timezone.utc).isoformat()}
    client.put_object(
        Bucket=bucket,
        Key=_ledger_key(key),
        Body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def ledger_is_terminal(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    return entry.get("status") in ("started", "succeeded", "running", "deferred")


def _images_folder(schedule: ChannelSchedule, settings: ApiSettings) -> str:
    return (schedule.images_folder or schedule.category or settings.default_category).strip()


def _category(schedule: ChannelSchedule, settings: ApiSettings) -> str:
    return (schedule.category or settings.default_category).strip()


def evaluate_resources(
    client,
    bucket: str,
    schedule: ChannelSchedule,
    settings: ApiSettings,
    *,
    inventory_cache: dict[str, dict[str, int]] | None = None,
    extend_pending_cache: dict[str, int] | None = None,
) -> dict[str, Any]:
    category = _category(schedule, settings)
    images_folder = _images_folder(schedule, settings)
    inv_cache = inventory_cache if inventory_cache is not None else {}
    ext_cache = extend_pending_cache if extend_pending_cache is not None else {}

    if category not in inv_cache:
        inv_cache[category] = category_inventory(client, bucket, category)
    inv = inv_cache[category]

    if category not in ext_cache:
        cfg = r2_config_from_env(category=category)
        ext_cache[category] = count_pending_r2_sources(client, cfg)
    extend_pending = ext_cache[category]

    backgrounds = int(inv.get("backgrounds_available") or 0)
    if images_folder != category:
        if images_folder not in inv_cache:
            inv_cache[images_folder] = category_inventory(client, bucket, images_folder)
        inv_folder = inv_cache[images_folder]
        backgrounds = int(inv_folder.get("backgrounds_available") or backgrounds)
    music_tracks = int(inv.get("music_mp3s") or 0)
    blockers: list[str] = []
    if music_tracks <= 0:
        blockers.append("no_music_tracks")
    if backgrounds < schedule.min_backgrounds:
        blockers.append("low_backgrounds")
    ready = not blockers
    if backgrounds < schedule.min_backgrounds and extend_pending > 0 and schedule.auto_extend:
        blockers.append("extend_recommended")
    return {
        "category": category,
        "images_folder": images_folder,
        "backgrounds_available": backgrounds,
        "extend_pending": extend_pending,
        "music_tracks": music_tracks,
        "min_backgrounds": schedule.min_backgrounds,
        "ready": ready,
        "blockers": blockers,
    }


def preview_schedule(schedule: ChannelSchedule, *, now_utc: datetime | None = None, limit: int = 8) -> list[dict[str, Any]]:
    tz = ZoneInfo(schedule.timezone)
    now_local = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    upcoming: list[dict[str, Any]] = []
    for offset in range(14):
        day_local = now_local.date() + timedelta(days=offset)
        dow = day_local.weekday()
        day_index = (dow + 1) % 7
        day = schedule.days[day_index]
        if not day.enabled:
            continue
        assemble_at = day.assemble_at or schedule.default_assemble_at
        dt = datetime.combine(day_local, _parse_local_time(assemble_at), tzinfo=tz)
        if dt < now_local:
            continue
        upcoming.append(
            {
                "at_local": dt.isoformat(),
                "day_name": DAY_NAMES[day_index],
                "assemble_at": assemble_at,
                "upload_at": resolved_upload_at(day, schedule),
                "slot_key": slot_key(schedule.channel, day_local, day_index, assemble_at),
            }
        )
        if len(upcoming) >= limit:
            break
    return upcoming


def _active_days_summary(schedule: ChannelSchedule) -> list[str]:
    rows: list[str] = []
    for i, day in enumerate(schedule.days):
        if not day.enabled:
            continue
        assemble = day.assemble_at or schedule.default_assemble_at
        upload = resolved_upload_at(day, schedule)
        rows.append(f"{DAY_ABBR[i]} {assemble}→{upload}")
    return rows


def schedules_overview(
    client,
    bucket: str,
    settings: ApiSettings,
    *,
    upcoming_limit: int = 25,
    runs_limit: int = 40,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate all channel schedules, upcoming slots, and recent cron ledger entries."""
    schedules = list_schedules(client, bucket)
    channels: list[dict[str, Any]] = []
    all_upcoming: list[dict[str, Any]] = []
    inventory_cache: dict[str, dict[str, int]] = {}
    extend_pending_cache: dict[str, int] = {}

    for sched in schedules:
        upcoming = preview_schedule(sched, now_utc=now_utc, limit=6)
        resources = evaluate_resources(
            client,
            bucket,
            sched,
            settings,
            inventory_cache=inventory_cache,
            extend_pending_cache=extend_pending_cache,
        )
        next_slot = upcoming[0] if upcoming else None
        active_days = _active_days_summary(sched)
        channel_upcoming = [
            {
                **slot,
                "channel": sched.channel,
                "timezone": sched.timezone,
                "schedule_enabled": sched.enabled,
            }
            for slot in upcoming
        ]
        channels.append(
            {
                "channel": sched.channel,
                "enabled": sched.enabled,
                "timezone": sched.timezone,
                "images_folder": _images_folder(sched, settings),
                "category": _category(sched, settings),
                "active_days": active_days,
                "active_day_count": len(active_days),
                "default_assemble_at": sched.default_assemble_at,
                "default_upload_at": resolved_upload_at(
                    DaySlot(enabled=True, assemble_at=sched.default_assemble_at),
                    sched,
                ),
                "duration_min": sched.duration_min,
                "queue_youtube": sched.queue_youtube,
                "upload_schedule_publish": sched.upload_schedule_publish,
                "upload_privacy": sched.upload_privacy,
                "resources_ready": resources.get("ready"),
                "backgrounds_available": resources.get("backgrounds_available"),
                "blockers": resources.get("blockers") or [],
                "next_slot": next_slot,
                "upcoming": channel_upcoming,
            }
        )
        all_upcoming.extend(channel_upcoming)

    channels.sort(key=lambda row: (row.get("channel") or "").lower())
    all_upcoming.sort(
        key=lambda row: (
            (row.get("channel") or "").lower(),
            row.get("at_local") or "",
        )
    )
    runs = list_schedule_runs(client, bucket, limit=runs_limit)

    return {
        "cron": {
            "endpoint": "/v1/cron/run-schedules",
            "poll_minutes": 15,
            "match_window_minutes": DEFAULT_WINDOW_MINUTES,
        },
        "channels": channels,
        "channel_count": len(channels),
        "upcoming": all_upcoming[: max(upcoming_limit, 0)],
        "recent_runs": runs,
    }


def _patch_meta_schedule_slot(client, bucket: str, execution_id: str, slot_key_value: str) -> None:
    from music_assembler.job_progress import meta_key, read_meta_json

    meta = read_meta_json(client, bucket, execution_id) or {"execution_id": execution_id}
    meta["schedule_slot_key"] = slot_key_value
    meta["job_type"] = meta.get("job_type") or "assembly"
    client.put_object(
        Bucket=bucket,
        Key=meta_key(execution_id),
        Body=json.dumps(meta).encode("utf-8"),
        ContentType="application/json",
    )


def start_scheduled_assembly(
    client,
    bucket: str,
    settings: ApiSettings,
    schedule: ChannelSchedule,
    slot: dict[str, Any],
    *,
    new_execution_id,
) -> dict[str, Any]:
    category = _category(schedule, settings)
    images_folder = _images_folder(schedule, settings)
    execution_id = new_execution_id()
    write_meta_json(
        client,
        bucket,
        execution_id,
        category=category,
        channel=schedule.channel,
        images_folder=images_folder,
        duration_min=schedule.duration_min,
        variance_min=schedule.variance_min,
        thumbnail_text=schedule.thumbnail_text,
        job_type="assembly",
    )
    _patch_meta_schedule_slot(client, bucket, execution_id, slot["slot_key"])
    write_progress_json(
        client,
        bucket,
        execution_id,
        pct=0,
        stage=f"Scheduled assembly ({slot['day_name']} {slot['assemble_at']})…",
        category=category,
        status="running",
        extra={"schedule_slot_key": slot["slot_key"]},
    )
    publish_at = slot_publish_at_utc(slot, schedule)
    result = gcp_jobs.start_assembly_job(
        settings,
        execution_id=execution_id,
        category=category,
        channel=schedule.channel,
        images_folder=images_folder,
        thumbnail_text=schedule.thumbnail_text,
        duration_min=schedule.duration_min,
        variance_min=schedule.variance_min,
        queue_youtube=schedule.queue_youtube,
        upload_privacy=schedule.upload_privacy,
        publish_at=publish_at,
        upload_tags=schedule.upload_tags or None,
        upload_category_id=schedule.upload_category_id,
        upload_made_for_kids=schedule.upload_made_for_kids,
    )
    gcp_id = result.get("gcp_execution_id")
    if gcp_id:
        patch_meta_gcp_execution_id(client, bucket, execution_id, gcp_id)
    write_ledger(
        client,
        bucket,
        slot["slot_key"],
        {
            "status": "started",
            "execution_id": execution_id,
            "channel": schedule.channel,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "assemble_at": slot["assemble_at"],
            "upload_at": slot.get("upload_at"),
        },
    )
    return {"execution_id": execution_id, "gcp_execution_id": gcp_id, "slot_key": slot["slot_key"]}


def run_due_schedules(
    client,
    bucket: str,
    settings: ApiSettings,
    *,
    now_utc: datetime | None = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    dry_run: bool = False,
    new_execution_id,
    start_extend_fn,
) -> dict[str, Any]:
    """Evaluate all schedules; start assembly or record skip/defer."""
    results: list[dict[str, Any]] = []
    for schedule in list_schedules(client, bucket):
        for slot in due_slots(schedule, now_utc=now_utc, window_minutes=window_minutes):
            entry = read_ledger(client, bucket, slot["slot_key"])
            if ledger_is_terminal(entry):
                results.append({"slot_key": slot["slot_key"], "action": "skipped", "reason": "already_started"})
                continue
            resources = evaluate_resources(client, bucket, schedule, settings)
            if not resources["ready"]:
                if schedule.auto_extend and resources["extend_pending"] > 0 and "low_backgrounds" in resources["blockers"]:
                    if dry_run:
                        results.append({"slot_key": slot["slot_key"], "action": "would_defer_extend", "resources": resources})
                        continue
                    ext_id = new_execution_id()
                    try:
                        start_extend_fn(client, bucket, settings, execution_id=ext_id, category=resources["category"], max_images=3, force=False)
                        write_ledger(
                            client,
                            bucket,
                            slot["slot_key"],
                            {"status": "deferred", "reason": "auto_extend", "extend_execution_id": ext_id, "channel": schedule.channel},
                        )
                        results.append({"slot_key": slot["slot_key"], "action": "deferred_extend", "extend_execution_id": ext_id})
                    except Exception as exc:
                        write_ledger(
                            client,
                            bucket,
                            slot["slot_key"],
                            {"status": "skipped", "reason": f"extend_failed:{exc}", "channel": schedule.channel},
                        )
                        results.append({"slot_key": slot["slot_key"], "action": "skipped", "reason": str(exc)})
                    continue
                if dry_run:
                    results.append({"slot_key": slot["slot_key"], "action": "would_skip", "resources": resources})
                    continue
                write_ledger(
                    client,
                    bucket,
                    slot["slot_key"],
                    {"status": "skipped", "reason": ",".join(resources["blockers"]), "channel": schedule.channel},
                )
                results.append({"slot_key": slot["slot_key"], "action": "skipped", "reason": resources["blockers"]})
                continue
            if dry_run:
                results.append({"slot_key": slot["slot_key"], "action": "would_start", "slot": slot})
                continue
            try:
                started = start_scheduled_assembly(
                    client, bucket, settings, schedule, slot, new_execution_id=new_execution_id
                )
                results.append({"slot_key": slot["slot_key"], "action": "started", **started})
            except Exception as exc:
                write_ledger(
                    client,
                    bucket,
                    slot["slot_key"],
                    {"status": "failed", "reason": str(exc), "channel": schedule.channel},
                )
                results.append({"slot_key": slot["slot_key"], "action": "failed", "reason": str(exc)})
    return {
        "evaluated_at": (now_utc or datetime.now(timezone.utc)).isoformat(),
        "window_minutes": window_minutes,
        "dry_run": dry_run,
        "results": results,
    }
