"""Asynchronous scheduler that records NHK radio broadcasts via ffmpeg."""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

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
    LOGGER.debug("Fetching broadcast events for series '%s'", series_id)
    events = await fetch_broadcast_events(session, series_id)
    LOGGER.debug("Fetched %d events for series '%s'", len(events), series_id)

    LOGGER.debug("Fetching stream catalog for available areas")
    catalogs = await fetch_stream_catalog(session)
    LOGGER.debug("Stream catalog contains %d area entries", len(catalogs))

    keys = [area_key]
    lowered = area_key.lower()
    if lowered not in keys:
        keys.append(lowered)
    LOGGER.debug("Looking up stream catalog for area '%s'", area_key)
    catalog = None
    for key in keys:
        catalog = catalogs.get(key)
        if catalog is not None:
            LOGGER.debug("Found catalog for key '%s'", key)
            break
    if catalog is None:
        raise ValueError(f"Area '{area_key}' not found in NHK stream catalog")

    now = datetime.now(tz=timezone.utc)
    plans: List[RecordingPlan] = []
    accepted_area_ids = {catalog.area_key.strip().lower()}
    if catalog.area_slug:
        accepted_area_ids.add(catalog.area_slug.strip().lower())

    LOGGER.debug(
        "Accepted area identifiers for catalog: %s",
        sorted(accepted_area_ids),
    )

    for event in events:
        LOGGER.debug(
            "Evaluating event '%s' (%s-%s) area_id=%s",
            event.title,
            event.start.isoformat(),
            event.end.isoformat() if event.end else "?",
            event.area_id,
        )
        event_area_id = event.area_id.strip().lower()
        if event_area_id not in accepted_area_ids:
            LOGGER.debug(
                "Skipping event '%s' due to unmatched area_id '%s'",
                event.title,
                event.area_id,
            )
            continue
        if earliest_start and event.start < earliest_start:
            LOGGER.debug(
                "Skipping event '%s' because start %s is before earliest_start %s",
                event.title,
                event.start.isoformat(),
                earliest_start.isoformat(),
            )
            continue
        if event.start < now:
            LOGGER.debug(
                "Skipping event '%s' because start %s is in the past (now=%s)",
                event.title,
                event.start.isoformat(),
                now.isoformat(),
            )
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
    poll_interval_seconds: int = 900,
) -> None:
    LOGGER.debug(
        "run_scheduler called with series_id=%s area=%s output_dir=%s lead_in=%ss tail_out=%ss default_duration_minutes=%s max_events=%s earliest_start=%s dry_run=%s",
        series_id,
        area,
        output_dir,
        lead_in_seconds,
        tail_out_seconds,
        default_duration_minutes,
        max_events,
        earliest_start.isoformat() if earliest_start else None,
        dry_run,
    )

    area = area.strip()
    lead_in = timedelta(seconds=max(lead_in_seconds, 0))
    tail_out = timedelta(seconds=max(tail_out_seconds, 0))
    default_duration = (
        timedelta(minutes=default_duration_minutes)
        if default_duration_minutes and default_duration_minutes > 0
        else None
    )

    output_dir = output_dir.expanduser()

    poll_interval = max(poll_interval_seconds, 0)
    scheduled_event_ids: Set[str] = set()
    active_tasks: Dict[asyncio.Task, RecordingPlan] = {}

    warned_no_events = False

    async with aiohttp.ClientSession() as session:
        try:
            while True:
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

                new_plans: List[RecordingPlan] = []
                for plan in plans:
                    event_id = plan.event.broadcast_event_id
                    if event_id in scheduled_event_ids:
                        LOGGER.debug(
                            "Event '%s' already scheduled, skipping",
                            plan.event.title,
                        )
                        continue
                    scheduled_event_ids.add(event_id)
                    new_plans.append(plan)

                if new_plans:
                    warned_no_events = False
                    LOGGER.info("Prepared %d new recording plan(s)", len(new_plans))
                    for plan in new_plans:
                        LOGGER.info(
                            "Event '%s' scheduled at %s",
                            plan.event.title,
                            plan.start_time.isoformat(),
                        )
                        task = asyncio.create_task(
                            execute_recording(
                                plan,
                                ffmpeg_path=ffmpeg_path,
                                log_level=ffmpeg_log_level,
                                dry_run=dry_run,
                            )
                        )
                        active_tasks[task] = plan
                elif not active_tasks and not warned_no_events:
                    LOGGER.warning(
                        "No future events found for series %s in area %s",
                        series_id,
                        area,
                    )
                    warned_no_events = True

                # Remove any tasks that completed without going through asyncio.wait
                for task in list(active_tasks):
                    if task.done():
                        plan = active_tasks.pop(task)
                        try:
                            await task
                        except asyncio.CancelledError:
                            LOGGER.info(
                                "Recording task for '%s' was cancelled",
                                plan.event.title,
                            )
                        except Exception as exc:  # noqa: BLE001
                            LOGGER.exception(
                                "Recording task for '%s' failed: %s",
                                plan.event.title,
                                exc,
                            )

                if active_tasks:
                    warned_no_events = False
                    done, _ = await asyncio.wait(
                        tuple(active_tasks.keys()),
                        timeout=poll_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        plan = active_tasks.pop(task, None)
                        try:
                            await task
                        except asyncio.CancelledError:
                            if plan:
                                LOGGER.info(
                                    "Recording task for '%s' was cancelled",
                                    plan.event.title,
                                )
                        except Exception as exc:  # noqa: BLE001
                            if plan:
                                LOGGER.exception(
                                    "Recording task for '%s' failed: %s",
                                    plan.event.title,
                                    exc,
                                )
                            else:
                                LOGGER.exception("Recording task failed: %s", exc)
                else:
                    await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            for task, plan in active_tasks.items():
                if not task.done():
                    task.cancel()
                    LOGGER.debug(
                        "Cancelling recording task for '%s'", plan.event.title
                    )
            if active_tasks:
                await asyncio.gather(*tuple(active_tasks.keys()), return_exceptions=True)
            raise
