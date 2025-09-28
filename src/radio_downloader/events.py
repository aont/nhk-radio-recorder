"""Utilities for fetching and parsing NHK broadcast events."""

from __future__ import annotations

import json
from typing import Any, Iterable, List, Optional

import aiohttp

from .models import NHKEvent
from .utils import JP_TZ, any_key, parse_iso8601

START_KEYS: Iterable[str] = (
    "start_time",
    "startTime",
    "startDateTime",
    "startDate",
    "start",
)
END_KEYS: Iterable[str] = (
    "end_time",
    "endTime",
    "endDateTime",
    "endDate",
    "end",
)
TITLE_KEYS: Iterable[str] = (
    "title",
    "event_title",
    "program_title",
    "name",
)
SERVICE_KEYS: Iterable[str] = (
    "service",
    "serviceId",
    "broadcastServiceId",
    "onair_service",
    "channel",
)
AREA_KEYS: Iterable[str] = (
    "area",
    "areaKey",
    "areakey",
    "region",
    "regionCode",
)
ID_KEYS: Iterable[str] = (
    "broadcastEventId",
    "event_id",
    "id",
    "be_id",
    "item_id",
    "content_id",
)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _normalize_service(value: Any) -> Optional[str]:
    """Convert loosely formatted service identifiers into ``r1``/``r2``/``fm``."""

    if isinstance(value, dict):
        for key in ("id", "name", "serviceId", "service", "channel"):
            if key in value:
                normalized = _normalize_service(value[key])
                if normalized:
                    return normalized
        return None

    if isinstance(value, str):
        lower = value.lower()
        if "r1" in lower:
            return "r1"
        if "r2" in lower or "rs" in lower:
            return "r2"
        if "fm" in lower or "r3" in lower:
            return "fm"
    return None


def _extract_service(candidate: Any) -> Optional[str]:
    """Search ``candidate`` recursively for a recognizable service identifier."""

    if isinstance(candidate, dict):
        for key in SERVICE_KEYS:
            if key in candidate:
                normalized = _normalize_service(candidate[key])
                if normalized:
                    return normalized
        for value in candidate.values():
            found = _extract_service(value)
            if found:
                return found
    elif isinstance(candidate, list):
        for item in candidate:
            found = _extract_service(item)
            if found:
                return found
    return None


def extract_events_from_json(payload: Any) -> List[NHKEvent]:
    """Extract :class:`NHKEvent` objects from an arbitrary JSON payload."""

    events: List[NHKEvent] = []
    for candidate in _walk(payload):
        if not isinstance(candidate, dict):
            continue
        start_raw = any_key(candidate, START_KEYS)
        end_raw = any_key(candidate, END_KEYS)
        if start_raw is None or end_raw is None:
            continue
        start = parse_iso8601(start_raw, default_tz=JP_TZ)
        end = parse_iso8601(end_raw, default_tz=JP_TZ)
        if not start or not end or end <= start:
            continue

        title = any_key(candidate, TITLE_KEYS) or "NHK Radio"

        service = _extract_service(candidate)

        area = any_key(candidate, AREA_KEYS)
        if isinstance(area, dict):
            area = area.get("id") or area.get("name")
        if isinstance(area, str):
            area = area.lower()
        else:
            area = None

        event_id = any_key(candidate, ID_KEYS) or ""
        if isinstance(event_id, dict):
            event_id = event_id.get("id") or ""

        events.append(
            NHKEvent(
                event_id=str(event_id),
                title=str(title),
                start=start,
                end=end,
                service=service,
                area=area,
            )
        )
    return events


def _is_no_schedule_payload(payload: Any) -> bool:
    """Return ``True`` when *payload* represents a "no schedule" response."""

    if not isinstance(payload, dict):
        return False

    error = payload.get("error")
    if not isinstance(error, dict):
        return False

    status = error.get("statuscode")
    message = error.get("message")

    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return False

    if status_int != 404:
        return False

    if isinstance(message, str):
        normalized = message.strip().lower()
        if normalized in {"not found", "not found."}:
            return True

    return False


async def fetch_events(session: aiohttp.ClientSession, url: str) -> List[NHKEvent]:
    """Fetch a broadcast schedule JSON and convert it into ``NHKEvent`` records."""

    headers = {"User-Agent": "nhk-radio-recorder/1.0 (+asyncio)"}
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)

    events = extract_events_from_json(payload)
    if not events:
        if _is_no_schedule_payload(payload):
            return []
        snippet = json.dumps(payload, ensure_ascii=False)[:500]
        raise RuntimeError(
            "Failed to extract events from the broadcast schedule JSON: "
            f"{url}\npayload snippet: {snippet} ..."
        )
    return events
