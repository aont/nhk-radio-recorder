"""Asynchronous scheduler that records NHK radio broadcasts via ffmpeg."""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import aiohttp

import sleep_absolute

from .nhk import BroadcastEvent, StreamCatalog, fetch_broadcast_events, fetch_stream_catalog

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingPlan:
    """Plan describing how to record a broadcast."""

    event: BroadcastEvent
    stream_catalog: StreamCatalog
    output_path: Path
    lead_in: timedelta
    tail_out: timedelta
    default_duration: Optional[timedelta]

    @property
    def stream_url(self) -> Optional[str]:
        return self.stream_catalog.get_stream_url(self.event.service_id)

    @property
    def start_time(self) -> datetime:
        return self.event.start - self.lead_in

    @property
    def stop_time(self) -> datetime:
        duration = self.event.duration_seconds
        if duration is None and self.default_duration is not None:
            duration = self.default_duration.total_seconds()
        if duration is None:
            raise ValueError("Cannot determine duration for event without end time")
        return self.event.start + timedelta(seconds=duration) + self.tail_out

    @property
    def record_duration(self) -> timedelta:
        return self.stop_time - self.start_time


async def wait_until(target: datetime) -> None:
    """Wait asynchronously until ``target`` using sleep-absolute when possible."""

    if target.tzinfo is None:
        raise ValueError("wait_until requires timezone-aware datetime")
    now = datetime.now(target.tzinfo)
    if target <= now:
        return
    try:
        await sleep_absolute.wait_until(target)
    except NotImplementedError:
        delay = (target - now).total_seconds()
        await asyncio.sleep(max(delay, 0))


def sanitize_filename(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", " ") else "_" for c in value)
    return " ".join(cleaned.split()) or "recording"


def build_output_path(base_dir: Path, event: BroadcastEvent, extension: str = ".m4a") -> Path:
    timestamp = event.start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    title = sanitize_filename(event.title or event.broadcast_event_id)
    filename = f"{timestamp}_{title}{extension}"
    return base_dir / filename


async def run_ffmpeg(command: List[str]) -> int:
    LOGGER.info("Running ffmpeg: %s", " ".join(shlex.quote(part) for part in command))
    process = await asyncio.create_subprocess_exec(*command)
    return await process.wait()


async def execute_recording(
    plan: RecordingPlan,
    ffmpeg_path: str,
    log_level: str = "error",
    dry_run: bool = False,
) -> None:
    stream_url = plan.stream_url
    if not stream_url:
        LOGGER.error("No stream URL found for service %s", plan.event.service_id)
        return

    try:
        record_duration = plan.record_duration
    except ValueError as exc:
        LOGGER.error("Cannot schedule '%s': %s", plan.event.title, exc)
        return

    if dry_run:
        LOGGER.info(
            "[DRY RUN] Would record '%s' from %s for %s",
            plan.event.title,
            stream_url,
            record_duration,
        )
        return

    await wait_until(plan.start_time)

    duration_seconds = record_duration.total_seconds()
    command = [
        ffmpeg_path,
        "-nostdin",
        "-y",
        "-loglevel",
        log_level,
        "-i",
        stream_url,
        "-c",
        "copy",
        "-t",
        f"{duration_seconds:.0f}",
        str(plan.output_path),
    ]

    plan.output_path.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info(
        "Starting recording for '%s' (%s → %s)",
        plan.event.title,
        plan.start_time.isoformat(),
        plan.stop_time.isoformat(),
    )
    return_code = await run_ffmpeg(command)
    if return_code == 0:
        LOGGER.info("Recording for '%s' completed successfully", plan.event.title)
    else:
        LOGGER.error("ffmpeg exited with status %s while recording '%s'", return_code, plan.event.title)


async def prepare_plans(
    session: aiohttp.ClientSession,
    series_id: str,
    area_key: str,
    output_dir: Path,
    lead_in: timedelta,
    tail_out: timedelta,
    default_duration: Optional[timedelta],
    max_events: Optional[int] = None,
    earliest_start: Optional[datetime] = None,
) -> List[RecordingPlan]:
    events = await fetch_broadcast_events(session, series_id)
    catalogs = await fetch_stream_catalog(session)

    keys = [area_key]
    lowered = area_key.lower()
    if lowered not in keys:
        keys.append(lowered)
    catalog = None
    for key in keys:
        catalog = catalogs.get(key)
        if catalog is not None:
            break
    if catalog is None:
        raise ValueError(f"Area '{area_key}' not found in NHK stream catalog")

    now = datetime.now(tz=timezone.utc)
    plans: List[RecordingPlan] = []
    accepted_area_ids = {catalog.area_key.strip().lower()}
    if catalog.area_slug:
        accepted_area_ids.add(catalog.area_slug.strip().lower())

    for event in events:
        event_area_id = event.area_id.strip().lower()
        if event_area_id not in accepted_area_ids:
            continue
        if earliest_start and event.start < earliest_start:
            continue
        if event.start < now:
            continue
        output_path = build_output_path(output_dir, event)
        plan = RecordingPlan(
            event=event,
            stream_catalog=catalog,
            output_path=output_path,
            lead_in=lead_in,
            tail_out=tail_out,
            default_duration=default_duration,
        )
        plans.append(plan)
        if max_events and len(plans) >= max_events:
            break
    return plans


async def schedule_recordings(
    plans: Iterable[RecordingPlan],
    ffmpeg_path: str,
    log_level: str,
    dry_run: bool = False,
) -> None:
    tasks = [
        asyncio.create_task(execute_recording(plan, ffmpeg_path, log_level, dry_run))
        for plan in plans
    ]
    if not tasks:
        LOGGER.warning("No recordings to schedule")
        return
    await asyncio.gather(*tasks)


async def run_scheduler(
    series_id: str,
    area: str,
    output_dir: Path,
    lead_in_seconds: int,
    tail_out_seconds: int,
    default_duration_minutes: Optional[int],
    max_events: Optional[int],
    earliest_start: Optional[datetime],
    ffmpeg_path: str,
    ffmpeg_log_level: str,
    dry_run: bool,
) -> None:
    area = area.strip()
    lead_in = timedelta(seconds=max(lead_in_seconds, 0))
    tail_out = timedelta(seconds=max(tail_out_seconds, 0))
    default_duration = (
        timedelta(minutes=default_duration_minutes)
        if default_duration_minutes and default_duration_minutes > 0
        else None
    )

    output_dir = output_dir.expanduser()

    async with aiohttp.ClientSession() as session:
        plans = await prepare_plans(
            session=session,
            series_id=series_id,
            area_key=area,
            output_dir=output_dir,
            lead_in=lead_in,
            tail_out=tail_out,
            default_duration=default_duration,
            max_events=max_events,
            earliest_start=earliest_start,
        )

    if not plans:
        LOGGER.warning("No future events found for series %s in area %s", series_id, area)
        return

    LOGGER.info("Prepared %d recording plan(s)", len(plans))
    for plan in plans:
        LOGGER.info(
            "Event '%s' scheduled at %s",
            plan.event.title,
            plan.start_time.isoformat(),
        )

    await schedule_recordings(plans, ffmpeg_path=ffmpeg_path, log_level=ffmpeg_log_level, dry_run=dry_run)
