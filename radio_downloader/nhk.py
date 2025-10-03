"""Utilities for fetching NHK radio scheduling and streaming information."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

import aiohttp

__all__ = [
    "BroadcastEvent",
    "StreamCatalog",
    "MusicArtist",
    "MusicItem",
    "fetch_broadcast_events",
    "fetch_stream_catalog",
]


@dataclass(frozen=True)
class BroadcastEvent:
    """Representation of a scheduled radio broadcast."""

    broadcast_event_id: str
    title: str
    description: Optional[str]
    start: datetime
    end: Optional[datetime]
    service_id: str
    area_id: str
    detailed_description: Optional[Dict[str, str]] = None
    music_list: Optional[List["MusicItem"]] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        """Return the planned duration in seconds, if available."""

        if self.end is None:
            return None
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class StreamCatalog:
    """Mapping between area identifiers and stream URLs."""

    area_slug: str
    area_name: str
    area_key: str
    station_id: Optional[str]
    streams: Dict[str, str]

    def get_stream_url(self, service_id: str) -> Optional[str]:
        key = service_id.lower()
        if key == "r3":
            key = "fm"
        return self.streams.get(key)


@dataclass(frozen=True)
class MusicArtist:
    """Performer or contributor associated with a music item."""

    name: str
    role: Optional[str] = None
    part: Optional[str] = None


@dataclass(frozen=True)
class MusicItem:
    """Description of a piece of music listed for a broadcast."""

    name: Optional[str]
    nameruby: Optional[str]
    lyricist: Optional[str]
    composer: Optional[str]
    arranger: Optional[str]
    location: Optional[str]
    provider: Optional[str]
    label: Optional[str]
    duration: Optional[str]
    code: Optional[str]
    by_artist: List[MusicArtist]


_EVENT_API_TEMPLATE = "https://api.nhk.jp/r7/f/broadcastevent/rs"
_STREAM_CONFIG_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"


async def _json_request(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def fetch_broadcast_events(
    session: aiohttp.ClientSession,
    series_id: str,
) -> List[BroadcastEvent]:
    """Fetch scheduled broadcast events for the provided series identifier."""

    query_to = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    url = f"{_EVENT_API_TEMPLATE}/{series_id}.json?to={query_to}&status=scheduled"
    try:
        payload = await _json_request(session, url)
    except aiohttp.ClientResponseError as exc:
        if exc.status == 404:
            return []
        raise

    error_block = payload.get("error")
    if isinstance(error_block, dict) and error_block.get("statuscode") == 404:
        return []

    events: List[BroadcastEvent] = []

    def _clean_text(value: Optional[str]) -> Optional[str]:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    def _parse_music_list(raw_list: Iterable[dict]) -> List[MusicItem]:
        music_items: List[MusicItem] = []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            artists: List[MusicArtist] = []
            artists_raw = entry.get("byArtist") or []
            if isinstance(artists_raw, Iterable) and not isinstance(
                artists_raw, (str, bytes, dict)
            ):
                for artist in artists_raw:
                    if not isinstance(artist, dict):
                        continue
                    name = _clean_text(artist.get("name"))
                    if not name:
                        continue
                    artists.append(
                        MusicArtist(
                            name=name,
                            role=_clean_text(artist.get("role")),
                            part=_clean_text(artist.get("part")),
                        )
                    )
            music_items.append(
                MusicItem(
                    name=_clean_text(entry.get("name")),
                    nameruby=_clean_text(entry.get("nameruby")),
                    lyricist=_clean_text(entry.get("lyricist")),
                    composer=_clean_text(entry.get("composer")),
                    arranger=_clean_text(entry.get("arranger")),
                    location=_clean_text(entry.get("location")),
                    provider=_clean_text(entry.get("provider")),
                    label=_clean_text(entry.get("label")),
                    duration=_clean_text(entry.get("duration")),
                    code=_clean_text(entry.get("code")),
                    by_artist=artists,
                )
            )
        return music_items

    for item in payload.get("result", []):
        start_raw = item.get("startDate")
        if not start_raw:
            continue
        end_raw = item.get("endDate")
        identifier = item.get("identifierGroup", {})
        service_id = identifier.get("serviceId")
        area_id = identifier.get("areaId")
        if not service_id or not area_id:
            continue
        try:
            start_time = datetime.fromisoformat(start_raw)
        except ValueError:
            continue
        end_time: Optional[datetime] = None
        if end_raw:
            try:
                end_time = datetime.fromisoformat(end_raw)
            except ValueError:
                end_time = None
        detailed_description_raw = item.get("detailedDescription")
        detailed_description: Optional[Dict[str, str]] = None
        if isinstance(detailed_description_raw, dict):
            cleaned: Dict[str, str] = {}
            for key, value in detailed_description_raw.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                stripped = value.strip()
                if stripped:
                    cleaned[key] = stripped
            if cleaned:
                detailed_description = cleaned

        music_list: Optional[List[MusicItem]] = None
        misc = item.get("misc")
        if isinstance(misc, dict):
            raw_music_list = misc.get("musicList")
            if isinstance(raw_music_list, Iterable) and not isinstance(
                raw_music_list, (str, bytes, dict)
            ):
                parsed_music = _parse_music_list(raw_music_list)
                if parsed_music:
                    music_list = parsed_music

        events.append(
            BroadcastEvent(
                broadcast_event_id=identifier.get("broadcastEventId", ""),
                title=item.get("name", ""),
                description=item.get("description"),
                start=start_time,
                end=end_time,
                service_id=service_id,
                area_id=area_id,
                detailed_description=detailed_description,
                music_list=music_list,
            )
        )
    events.sort(key=lambda event: event.start)
    return events


async def fetch_stream_catalog(
    session: aiohttp.ClientSession,
) -> Dict[str, StreamCatalog]:
    """Fetch mapping from NHK area identifiers to stream URLs."""

    async with session.get(_STREAM_CONFIG_URL) as response:
        response.raise_for_status()
        text = await response.text()

    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    catalogs: Dict[str, StreamCatalog] = {}
    for data_node in root.findall(".//data"):
        area_name = (data_node.findtext("areajp") or "").strip()
        area_slug = (data_node.findtext("area") or "").strip()
        area_key = (data_node.findtext("areakey") or "").strip()
        station_id = (data_node.findtext("apikey") or "").strip() or None
        streams = {
            "r1": (data_node.findtext("r1hls") or "").strip(),
            "r2": (data_node.findtext("r2hls") or "").strip(),
            "fm": (data_node.findtext("fmhls") or "").strip(),
        }
        streams = {k: v for k, v in streams.items() if v}
        if not streams or not area_key:
            continue
        catalog = StreamCatalog(
            area_slug=area_slug,
            area_name=area_name,
            area_key=area_key,
            station_id=station_id,
            streams=streams,
        )
        catalogs[area_key] = catalog
        if area_slug:
            catalogs.setdefault(area_slug, catalog)
    return catalogs
