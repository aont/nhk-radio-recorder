from __future__ import annotations

import asyncio
import argparse
import contextlib
import json
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from xml.etree import ElementTree

import aiosqlite
from aiohttp import ClientSession, ClientTimeout, web
from sleep_absolute import wait_until
from asyncio.subprocess import PIPE

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = BASE_DIR / "recordings"
FRONTEND_DIR = BASE_DIR / "frontend"
RESERVATIONS_FILE = DATA_DIR / "reservations.json"
RECORDINGS_FILE = DATA_DIR / "recordings.json"
SERIES_CACHE_FILE = DATA_DIR / "series_cache.json"
DATABASE_FILE = DATA_DIR / "app.sqlite3"

SERIES_URL_TMPL = "https://www.nhk.or.jp/radio-api/app/v1/web/series?kana={kana}"
SERIES_KANA_LIST = ("a", "k", "s", "t", "n", "h", "m", "y", "r", "w")
EVENT_URL_TMPL = "https://api.nhk.jp/r7/f/broadcastevent/rs/{series_key}.json?offset=0&size=10&to={to_time}&status=scheduled"
EVENT_LOOKAHEAD_DAYS = 7
CONFIG_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"
SERIES_CACHE_TTL = timedelta(hours=1)
SERIES_WATCH_EXPAND_INTERVAL_SECONDS = 60 * 60

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nhk-recorder")
DEBUG_LOG = False


class AsyncRLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._count = 0

    async def acquire(self) -> None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("AsyncRLock must be used in an asyncio task")
        if self._owner is current:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = current
        self._count = 1

    def release(self) -> None:
        current = asyncio.current_task()
        if current is None or self._owner is not current:
            raise RuntimeError("AsyncRLock released by non-owner")
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "AsyncRLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


RECORDINGS_LOCK = AsyncRLock()


@dataclass
class Reservation:
    id: str
    type: str  # single_event | series_watch
    created_at: str
    status: str  # pending | scheduled | done | failed | cancelled
    payload: dict[str, Any]


@dataclass
class Recording:
    id: str
    created_at: str
    status: str
    reservation_id: str | None
    series_id: int | None
    broadcast_event_id: str | None
    title: str
    service_id: str
    area_id: str
    start_date: str
    end_date: str
    hls_manifest: str
    metadata: dict[str, str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


async def init_db(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_data (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    await db.commit()


def _db_key(path: Path) -> str:
    if path == RESERVATIONS_FILE:
        return "reservations"
    if path == RECORDINGS_FILE:
        return "recordings"
    if path == SERIES_CACHE_FILE:
        return "series_cache"
    return path.name


async def _db_get_json(db: aiosqlite.Connection, key: str, default: Any) -> Any:
    async with db.execute("SELECT value FROM app_data WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        logger.warning("failed to decode db json: key=%s", key)
        return default


async def _db_set_json(db: aiosqlite.Connection, key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO app_data(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    await db.commit()


async def migrate_json_to_sqlite(db: aiosqlite.Connection) -> None:
    legacy_sources = (
        ("reservations", RESERVATIONS_FILE, []),
        ("recordings", RECORDINGS_FILE, []),
        ("series_cache", SERIES_CACHE_FILE, {"value": None, "expires_at": datetime.fromtimestamp(0, timezone.utc).isoformat()}),
    )
    for key, path, default in legacy_sources:
        async with db.execute("SELECT 1 FROM app_data WHERE key = ?", (key,)) as cur:
            exists = await cur.fetchone()
        if exists:
            continue
        value: Any = default
        if path.exists():
            with contextlib.suppress(Exception):
                value = json.loads(path.read_text(encoding="utf-8"))
        await _db_set_json(db, key, value)


async def load_series_cache(db: aiosqlite.Connection) -> dict[str, Any]:
    default = {"value": None, "expires_at": datetime.fromtimestamp(0, timezone.utc)}
    payload = await _db_get_json(db, "series_cache", None)
    if not isinstance(payload, dict):
        return default
    try:
        expires_at = datetime.fromisoformat(str(payload.get("expires_at", "")))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        value = payload.get("value")
        if not isinstance(value, list):
            value = None
        return {"value": value, "expires_at": expires_at}
    except Exception:
        logger.warning("failed to load series cache from sqlite")
        return default


async def persist_series_cache(db: aiosqlite.Connection, cache: dict[str, Any]) -> None:
    await _db_set_json(
        db,
        "series_cache",
        {"value": cache.get("value"), "expires_at": cache["expires_at"].isoformat()},
    )


async def read_json(db: aiosqlite.Connection, path: Path) -> list[dict[str, Any]]:
    payload = await _db_get_json(db, _db_key(path), [])
    if not isinstance(payload, list):
        return []
    return payload


async def write_json(db: aiosqlite.Connection, path: Path, payload: list[dict[str, Any]]) -> None:
    await _db_set_json(db, _db_key(path), payload)


class NHKClient:
    def __init__(self, session: ClientSession):
        self.session = session

    SERIES_CODE_PATTERN = re.compile(r"/rs/([A-Z0-9]+)/?", re.IGNORECASE)

    @classmethod
    def extract_series_key(cls, url: str) -> str | None:
        path = urlparse(url).path
        match = cls.SERIES_CODE_PATTERN.search(path)
        if match:
            return match.group(1).upper()
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else None

    async def resolve_series_code(self, url: str) -> str | None:
        direct = self.extract_series_key(url)
        if self.SERIES_CODE_PATTERN.search(urlparse(url).path):
            return direct
        try:
            if DEBUG_LOG:
                logger.info("[debug] resolve_series_code: HEAD %s", url)
            async with self.session.head(url, allow_redirects=False) as res:
                location = (res.headers.get("Location") or "").strip()
            if not location:
                return direct
            redirected = self.extract_series_key(location)
            if redirected:
                return redirected
        except Exception:
            if DEBUG_LOG:
                logger.exception("[debug] resolve_series_code failed: %s", url)
        return direct

    async def _get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        retries = [0.5, 1.5]
        for i in range(3):
            try:
                if DEBUG_LOG:
                    logger.info("[debug] GET JSON: %s (attempt=%d)", url, i + 1)
                async with self.session.get(url, headers=headers) as res:
                    if res.status >= 500 and i < 2:
                        await asyncio.sleep(retries[i])
                        continue
                    payload = await res.json(content_type=None)
                    if DEBUG_LOG:
                        logger.info(
                            "[debug] GET JSON done: status=%s keys=%s",
                            res.status,
                            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                        )
                    return res.status, payload
            except Exception:
                if DEBUG_LOG:
                    logger.exception("[debug] GET JSON failed: %s", url)
                if i == 2:
                    raise
                await asyncio.sleep(retries[i])
        raise RuntimeError("unreachable")

    async def _get_text(self, url: str) -> str:
        retries = [0.5, 1.5]
        for i in range(3):
            try:
                async with self.session.get(url) as res:
                    if res.status >= 500 and i < 2:
                        await asyncio.sleep(retries[i])
                        continue
                    res.raise_for_status()
                    return await res.text()
            except Exception:
                if i == 2:
                    raise
                await asyncio.sleep(retries[i])
        raise RuntimeError("unreachable")

    async def fetch_series(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for kana in SERIES_KANA_LIST:
            headers = {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "x-requested-with": "XMLHttpRequest",
                "Referer": f"https://www.nhk.or.jp/radio/programs/index.html?kana={kana}",
            }
            _, payload = await self._get_json(SERIES_URL_TMPL.format(kana=kana), headers)
            for item in payload.get("series", []):
                if not all(k in item and str(item[k]).strip() for k in ("id", "title", "url", "radio_broadcast")):
                    continue
                series_id = int(item["id"])
                if series_id in seen_ids:
                    continue
                seen_ids.add(series_id)
                broadcasts = [x.strip() for x in str(item["radio_broadcast"]).split(",") if x.strip()]
                series_url = str(item["url"]).strip()
                out.append(
                    {
                        "id": series_id,
                        "title": str(item["title"]).strip(),
                        "broadcasts": broadcasts,
                        "url": series_url,
                        "thumbnailUrl": (item.get("thumbnail_url") or "").strip() or None,
                        "scheduleText": (item.get("schedule") or "").strip() or None,
                        "areaName": (item.get("area") or "").strip() or None,
                    }
                )
        if DEBUG_LOG:
            logger.info("[debug] fetch_series: %d rows", len(out))
        return out

    async def fetch_events(self, series_key: str) -> list[dict[str, Any]]:
        to_time = (datetime.now() + timedelta(days=EVENT_LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M")
        url = EVENT_URL_TMPL.format(series_key=series_key, to_time=to_time)
        status, payload = await self._get_json(url)
        if DEBUG_LOG:
            logger.info(
                "[debug] fetch_events: series_key=%s lookahead_days=%s to_time=%s status=%s result_count=%s",
                series_key,
                EVENT_LOOKAHEAD_DAYS,
                to_time,
                status,
                len(payload.get("result", [])) if isinstance(payload, dict) else None,
            )
        if status == 404:
            return []
        if payload.get("error", {}).get("statuscode") == 404:
            return []
        out: list[dict[str, Any]] = []
        for ev in payload.get("result", []):
            ig = ev.get("identifierGroup", {})
            if not ev.get("startDate") or not ig.get("serviceId") or not ig.get("areaId"):
                continue
            try:
                start_dt = datetime.fromisoformat(ev["startDate"])
                end_dt = datetime.fromisoformat(ev["endDate"]) if ev.get("endDate") else start_dt + timedelta(minutes=30)
            except ValueError:
                continue
            dd = {k: str(v).strip() for k, v in (ev.get("detailedDescription") or {}).items() if str(v).strip()}
            about = ev.get("about") or {}
            part_of_series = about.get("partOfSeries") or {}
            genres = [
                g.get("name2") or g.get("name1")
                for g in ig.get("genre", [])
                if isinstance(g, dict) and (g.get("name1") or g.get("name2"))
            ]
            out.append(
                {
                    "name": ev.get("name", "Untitled"),
                    "description": ev.get("description"),
                    "startDate": start_dt.isoformat(),
                    "endDate": end_dt.isoformat(),
                    "duration": ev.get("duration"),
                    "broadcastEventId": ig.get("broadcastEventId"),
                    "serviceId": ig.get("serviceId"),
                    "areaId": ig.get("areaId"),
                    "serviceName": ((ev.get("publishedOn") or {}).get("name") or None),
                    "serviceDisplayName": ((ev.get("publishedOn") or {}).get("broadcastDisplayName") or None),
                    "location": ((ev.get("location") or {}).get("name") or None),
                    "eventUrl": ev.get("url") or None,
                    "episodeApiUrl": about.get("url") or None,
                    "episodeUrl": about.get("canonical") or None,
                    "seriesApiUrl": part_of_series.get("url") or None,
                    "seriesUrl": part_of_series.get("canonical") or None,
                    "radioEpisodeId": ig.get("radioEpisodeId"),
                    "radioSeriesId": ig.get("radioSeriesId"),
                    "genres": genres,
                    "detailedDescription": dd,
                    "musicList": ((ev.get("misc") or {}).get("musicList") or []),
                }
            )
        if DEBUG_LOG:
            logger.info("[debug] fetch_events filtered: %d rows", len(out))
        return out

    async def fetch_stream_catalog(self) -> dict[str, dict[str, Any]]:
        xml_text = await self._get_text(CONFIG_URL)
        root = ElementTree.fromstring(xml_text)
        out: dict[str, dict[str, Any]] = {}
        for data in root.findall(".//data"):
            area_key = (data.findtext("areakey") or "").strip()
            area_slug = (data.findtext("area") or "").strip()
            streams = {
                "r1": (data.findtext("r1hls") or "").strip(),
                "r2": (data.findtext("r2hls") or "").strip(),
                "fm": (data.findtext("fmhls") or "").strip(),
            }
            streams = {k: v for k, v in streams.items() if v}
            if not area_key or not streams:
                continue
            catalog = {
                "areaNameJp": (data.findtext("areajp") or "").strip() or None,
                "areaSlug": area_slug or None,
                "areaKey": area_key,
                "stationId": (data.findtext("apikey") or "").strip() or None,
                "streams": streams,
            }
            out[area_key] = catalog
            if area_slug:
                out[area_slug] = catalog
        return out


class RecorderService:
    def __init__(self, app: web.Application):
        self.app = app
        self.loop_task: asyncio.Task | None = None
        self.active_recording_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self.loop_task = asyncio.create_task(self.scheduler_loop())

    async def stop(self) -> None:
        if self.loop_task:
            self.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop_task
        for task in self.active_recording_tasks.values():
            task.cancel()
        if self.active_recording_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self.active_recording_tasks.values())
        self.active_recording_tasks.clear()

    async def scheduler_loop(self) -> None:
        last_series_expand = datetime.min.replace(tzinfo=timezone.utc)
        while True:
            try:
                now = utc_now()
                if (now - last_series_expand).total_seconds() >= SERIES_WATCH_EXPAND_INTERVAL_SECONDS:
                    await self._expand_series_watchers()
                    last_series_expand = now
                await self._run_due_recordings()
            except Exception as exc:
                logger.exception("Scheduler error: %s", exc)
            await asyncio.sleep(30)

    async def _expand_series_watchers(self) -> None:
        reservations = await read_json(self.app["db"], RESERVATIONS_FILE)
        changed = False
        for r in reservations:
            if r["type"] != "series_watch" or r["status"] != "pending":
                continue
            payload = r["payload"]
            seen = set(payload.setdefault("seen_broadcast_event_ids", []))
            series_key = str(payload.get("series_code") or payload["series_id"])
            events = await self.app["nhk"].fetch_events(series_key)
            for ev in events:
                beid = ev.get("broadcastEventId")
                if not beid or beid in seen:
                    continue
                if payload.get("area_id") and ev["areaId"] != payload["area_id"]:
                    continue
                reservations.append(
                    asdict(
                        Reservation(
                            id=str(uuid.uuid4()),
                            type="single_event",
                            created_at=utc_now().isoformat(),
                            status="pending",
                            payload={
                                "series_id": payload["series_id"],
                                "series_code": payload.get("series_code"),
                                "event": ev,
                                "from_series_watch": r["id"],
                                "metadata": build_reservation_metadata(payload["series_id"], payload.get("series_code"), ev),
                            },
                        )
                    )
                )
                seen.add(beid)
                changed = True
            payload["seen_broadcast_event_ids"] = sorted(seen)
        if changed:
            await write_json(self.app["db"], RESERVATIONS_FILE, reservations)

    async def _run_due_recordings(self) -> None:
        reservations = await read_json(self.app["db"], RESERVATIONS_FILE)
        changed = False
        for r in reservations:
            if r["type"] != "single_event" or r["status"] not in {"pending", "scheduled"}:
                continue
            event = r["payload"]["event"]
            start_dt = datetime.fromisoformat(event["startDate"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if r["id"] in self.active_recording_tasks:
                continue
            if r["status"] == "pending":
                r["status"] = "scheduled"
                changed = True
            task = asyncio.create_task(self._wait_and_execute_recording(r, start_dt))
            self.active_recording_tasks[r["id"]] = task
        if changed:
            await write_json(self.app["db"], RESERVATIONS_FILE, reservations)

    async def _wait_and_execute_recording(self, reservation: dict[str, Any], start_dt: datetime) -> None:
        try:
            if start_dt > utc_now():
                await wait_until(start_dt)
            await self.execute_recording(reservation)
        finally:
            self.active_recording_tasks.pop(reservation["id"], None)

    async def execute_recording(self, reservation: dict[str, Any]) -> None:
        event = reservation["payload"]["event"]
        service_id = event["serviceId"]
        stream_key = "fm" if service_id == "r3" else service_id
        logger.info(
            "recording start: reservation_id=%s broadcast_event_id=%s service_id=%s area_id=%s start=%s end=%s",
            reservation["id"],
            event.get("broadcastEventId"),
            service_id,
            event.get("areaId"),
            event.get("startDate"),
            event.get("endDate"),
        )
        catalogs = await self.app["nhk"].fetch_stream_catalog()
        catalog = catalogs.get(event["areaId"])
        if not catalog:
            logger.error(
                "recording failed before ffmpeg: reservation_id=%s reason=area_not_found area_id=%s available_keys=%s",
                reservation["id"],
                event["areaId"],
                sorted(catalogs.keys()),
            )
            await self._mark_reservation(reservation["id"], "failed")
            return
        stream_url = catalog["streams"].get(stream_key)
        if not stream_url:
            logger.error(
                "recording failed before ffmpeg: reservation_id=%s reason=stream_not_found stream_key=%s streams=%s",
                reservation["id"],
                stream_key,
                sorted(catalog["streams"].keys()),
            )
            await self._mark_reservation(reservation["id"], "failed")
            return

        rec_id = str(uuid.uuid4())
        rec_dir = RECORDINGS_DIR / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        manifest = rec_dir / "recording.m3u8"
        self._write_recording_debug_state(
            rec_dir,
            "prepared",
            {
                "reservation_id": reservation["id"],
                "broadcast_event_id": event.get("broadcastEventId"),
                "service_id": service_id,
                "stream_key": stream_key,
                "stream_url": stream_url,
                "start_date": event.get("startDate"),
                "end_date": event.get("endDate"),
            },
        )

        end_dt = datetime.fromisoformat(event["endDate"])
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            stream_url,
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "6",
            "-hls_list_size",
            "0",
            str(manifest),
        ]
        logger.info("recording ffmpeg start: reservation_id=%s rec_id=%s cmd=%s", reservation["id"], rec_id, cmd)
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=PIPE)
        self._write_recording_debug_state(rec_dir, "ffmpeg_started", {"pid": proc.pid, "command": cmd})
        if end_dt > utc_now():
            await wait_until(end_dt)

        if proc.returncode is None and proc.stdin:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(b"q")
                await proc.stdin.drain()
            proc.stdin.close()
        ret = await proc.wait()
        self._write_recording_debug_state(rec_dir, "ffmpeg_finished", {"return_code": ret})
        logger.info("recording ffmpeg finished: reservation_id=%s rec_id=%s return_code=%s", reservation["id"], rec_id, ret)

        if ret != 0:
            shutil.rmtree(rec_dir, ignore_errors=True)
            await self._mark_reservation(reservation["id"], "failed")
            return

        metadata = build_metadata_tags(event)
        async with RECORDINGS_LOCK:
            recordings = await read_json(self.app["db"], RECORDINGS_FILE)
            recordings.append(
                asdict(
                    Recording(
                        id=rec_id,
                        created_at=utc_now().isoformat(),
                        status="ready",
                        reservation_id=reservation["id"],
                        series_id=reservation["payload"].get("series_id"),
                        broadcast_event_id=event.get("broadcastEventId"),
                        title=event.get("name", "Untitled"),
                        service_id=service_id,
                        area_id=event["areaId"],
                        start_date=event["startDate"],
                        end_date=event["endDate"],
                        hls_manifest=f"/recordings/{rec_id}/recording.m3u8",
                        metadata=metadata,
                    )
                )
            )
            await write_json(self.app["db"], RECORDINGS_FILE, recordings)
        self._write_recording_debug_state(rec_dir, "index_written", {"recordings_count": len(recordings)})
        await self._mark_reservation(reservation["id"], "done")
        self._write_recording_debug_state(rec_dir, "reservation_done", {"reservation_id": reservation["id"]})
        logger.info("recording completed: reservation_id=%s rec_id=%s", reservation["id"], rec_id)

    def _write_recording_debug_state(self, rec_dir: Path, state: str, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "updated_at": utc_now().isoformat(),
            "state": state,
        }
        if extra:
            payload.update(extra)
        debug_file = rec_dir / "recording_debug.json"
        try:
            if debug_file.exists():
                current = json.loads(debug_file.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    current.update(payload)
                    payload = current
        except Exception:
            logger.exception("failed to read recording debug state: %s", debug_file)
        try:
            debug_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("failed to write recording debug state: %s", debug_file)

    async def _mark_reservation(self, reservation_id: str, status: str) -> None:
        reservations = await read_json(self.app["db"], RESERVATIONS_FILE)
        for r in reservations:
            if r["id"] == reservation_id:
                r["status"] = status
        await write_json(self.app["db"], RESERVATIONS_FILE, reservations)


def build_metadata_tags(event: dict[str, Any]) -> dict[str, str]:
    dd = event.get("detailedDescription") or {}
    description = dd.get("epg80") or dd.get("epg40") or event.get("description") or ""
    tags = {
        "title": event.get("name") or "Untitled",
        "description": description,
    }
    if dd.get("epg200"):
        tags["long_description"] = dd["epg200"]
    if dd.get("epgInformation"):
        tags["comment"] = dd["epgInformation"]
    remain = [f"{k}: {v}" for k, v in dd.items() if k not in {"epg80", "epg40", "epg200", "epgInformation"}]
    if remain:
        tags["nhk_detailed_description"] = "\n".join(remain)

    music_lines = []
    for m in event.get("musicList") or []:
        artists = [f"{a.get('name')}({a.get('role','')}/{a.get('part','')})" for a in m.get("byArtist", []) if a.get("name")]
        music_lines.append(f"{m.get('name','')} | {'; '.join(artists)}")
    if music_lines:
        tags["music_list"] = "\n".join(music_lines)
    return tags


def build_reservation_metadata(series_id: Any, series_code: Any, event: dict[str, Any]) -> dict[str, str]:
    return {
        "series_id": str(series_id or ""),
        "series_code": str(series_code or ""),
        "broadcast_event_id": str(event.get("broadcastEventId") or ""),
        "radio_series_id": str(event.get("radioSeriesId") or ""),
        "radio_episode_id": str(event.get("radioEpisodeId") or ""),
        "program_url": str(event.get("episodeUrl") or event.get("seriesUrl") or ""),
        "broadcast_event_info_url": str(event.get("eventUrl") or ""),
        "episode_api_url": str(event.get("episodeApiUrl") or ""),
        "series_api_url": str(event.get("seriesApiUrl") or ""),
    }


def build_series_watch_metadata(series_id: Any, series_code: Any, payload: dict[str, Any]) -> dict[str, str]:
    return {
        "series_id": str(series_id or ""),
        "series_code": str(series_code or ""),
        "series_title": str(payload.get("series_title") or ""),
        "series_area": str(payload.get("series_area") or ""),
        "series_schedule": str(payload.get("series_schedule") or ""),
        "program_url": str(payload.get("program_url") or ""),
        "series_thumbnail_url": str(payload.get("series_thumbnail_url") or ""),
    }


async def api_series(request: web.Request) -> web.Response:
    cache = request.app["series_cache"]
    now = utc_now()
    if cache["value"] is not None and cache["expires_at"] > now:
        return web.json_response(cache["value"])
    try:
        data = await request.app["nhk"].fetch_series()
        cache["value"] = data
        cache["expires_at"] = now + SERIES_CACHE_TTL
        await persist_series_cache(request.app["db"], cache)
        return web.json_response(data)
    except Exception as exc:
        logger.warning("series fetch failed: %s", exc)
        if cache["value"] is not None:
            return web.json_response(cache["value"])
        return web.json_response([])


async def api_events(request: web.Request) -> web.Response:
    nhk: NHKClient = request.app["nhk"]
    series_key = (request.query.get("series_code") or "").strip()
    if not series_key:
        series_url = (request.query.get("series_url") or "").strip()
        if series_url:
            series_key = (await nhk.resolve_series_code(series_url)) or ""
    if not series_key:
        series_key = (request.query.get("series_id") or "").strip()
    if not series_key:
        return web.json_response([])
    try:
        events = await nhk.fetch_events(series_key)
        if DEBUG_LOG:
            logger.info(
                "[debug] /api/events: series_key=%s lookahead_days=%s -> %d rows",
                series_key,
                EVENT_LOOKAHEAD_DAYS,
                len(events),
            )
        return web.json_response(events)
    except Exception as exc:
        logger.warning("event fetch failed: %s", exc)
        return web.json_response([])


async def api_reservations_get(request: web.Request) -> web.Response:
    return web.json_response(await read_json(request.app["db"], RESERVATIONS_FILE))


async def api_series_resolve(request: web.Request) -> web.Response:
    series_url = (request.query.get("series_url") or "").strip()
    if not series_url:
        return web.json_response({"seriesCode": None})
    try:
        series_code = await request.app["nhk"].resolve_series_code(series_url)
        return web.json_response({"seriesCode": series_code})
    except Exception as exc:
        logger.warning("series resolve failed: %s", exc)
        return web.json_response({"seriesCode": None})


async def api_reservations_post(request: web.Request) -> web.Response:
    payload = await request.json()
    reservation_payload = payload.setdefault("payload", {})
    if payload.get("type") == "single_event":
        reservation_payload["metadata"] = build_reservation_metadata(
            reservation_payload.get("series_id"),
            reservation_payload.get("series_code"),
            reservation_payload.get("event") or {},
        )
    if payload.get("type") == "series_watch":
        reservation_payload["metadata"] = build_series_watch_metadata(
            reservation_payload.get("series_id"),
            reservation_payload.get("series_code"),
            reservation_payload,
        )
    reservation = Reservation(
        id=str(uuid.uuid4()),
        type=payload["type"],
        created_at=utc_now().isoformat(),
        status="pending",
        payload=payload["payload"],
    )
    reservations = await read_json(request.app["db"], RESERVATIONS_FILE)
    reservations.append(asdict(reservation))
    await write_json(request.app["db"], RESERVATIONS_FILE, reservations)

    if payload.get("type") == "series_watch":
        recorder = request.app.get("recorder")
        if recorder:
            await recorder._expand_series_watchers()

    return web.json_response(asdict(reservation))


async def api_reservations_delete(request: web.Request) -> web.Response:
    rid = request.match_info["reservation_id"]
    reservations = [r for r in await read_json(request.app["db"], RESERVATIONS_FILE) if r["id"] != rid]
    await write_json(request.app["db"], RESERVATIONS_FILE, reservations)
    return web.json_response({"ok": True})


async def api_recordings_get(request: web.Request) -> web.Response:
    async with RECORDINGS_LOCK:
        return web.json_response(await read_json(request.app["db"], RECORDINGS_FILE))


async def _recording_by_id(db: aiosqlite.Connection, rec_id: str) -> dict[str, Any] | None:
    async with RECORDINGS_LOCK:
        for rec in await read_json(db, RECORDINGS_FILE):
            if rec["id"] == rec_id:
                return rec
    return None


async def api_recordings_patch_metadata(request: web.Request) -> web.Response:
    rec_id = request.match_info["recording_id"]
    payload = await request.json()
    async with RECORDINGS_LOCK:
        recordings = await read_json(request.app["db"], RECORDINGS_FILE)
        for rec in recordings:
            if rec["id"] == rec_id:
                rec["metadata"].update({k: str(v) for k, v in payload.items()})
        await write_json(request.app["db"], RECORDINGS_FILE, recordings)
    return web.json_response({"ok": True})


async def _convert_to_m4a(rec: dict[str, Any]) -> Path:
    rec_dir = RECORDINGS_DIR / rec["id"]
    m4a = rec_dir / "download.m4a"
    manifest = rec_dir / "recording.m3u8"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(manifest)]
    for k, v in rec.get("metadata", {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd += ["-c", "copy", str(m4a)]
    proc = await asyncio.create_subprocess_exec(*cmd)
    ret = await proc.wait()
    if ret != 0:
        raise RuntimeError("ffmpeg conversion failed")
    return m4a


async def api_recordings_download(request: web.Request) -> web.StreamResponse:
    rec_id = request.match_info["recording_id"]
    rec = await _recording_by_id(request.app["db"], rec_id)
    if not rec:
        raise web.HTTPNotFound()
    m4a = await _convert_to_m4a(rec)
    return web.FileResponse(m4a, headers={"Content-Disposition": f'attachment; filename="{rec_id}.m4a"'})


async def api_recordings_bulk_download(request: web.Request) -> web.StreamResponse:
    payload = await request.json()
    ids = payload.get("ids", [])
    tmpdir = Path(tempfile.mkdtemp(prefix="nhkzip-"))
    zippath = tmpdir / "recordings.zip"
    with zipfile.ZipFile(zippath, "w", compression=zipfile.ZIP_STORED) as zf:
        for rec_id in ids:
            rec = await _recording_by_id(request.app["db"], rec_id)
            if not rec:
                continue
            m4a = await _convert_to_m4a(rec)
            zf.write(m4a, arcname=f"{rec_id}.m4a")
    return web.FileResponse(zippath, headers={"Content-Disposition": 'attachment; filename="recordings.zip"'})


async def api_recordings_delete(request: web.Request) -> web.Response:
    rec_id = request.match_info["recording_id"]
    rec_dir = RECORDINGS_DIR / rec_id
    shutil.rmtree(rec_dir, ignore_errors=True)
    async with RECORDINGS_LOCK:
        recordings = [r for r in await read_json(request.app["db"], RECORDINGS_FILE) if r["id"] != rec_id]
        await write_json(request.app["db"], RECORDINGS_FILE, recordings)
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(FRONTEND_DIR / "index.html")


async def create_app() -> web.Application:
    ensure_dirs()
    timeout = ClientTimeout(total=10)
    session = ClientSession(timeout=timeout)
    db = await aiosqlite.connect(DATABASE_FILE)
    await init_db(db)
    await migrate_json_to_sqlite(db)

    app = web.Application()
    app["session"] = session
    app["db"] = db
    app["nhk"] = NHKClient(session)
    app["series_cache"] = await load_series_cache(db)

    app.router.add_get("/", index)
    app.router.add_static("/static", FRONTEND_DIR)
    app.router.add_static("/recordings", RECORDINGS_DIR)

    app.router.add_get("/api/series", api_series)
    app.router.add_get("/api/series/resolve", api_series_resolve)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/reservations", api_reservations_get)
    app.router.add_post("/api/reservations", api_reservations_post)
    app.router.add_delete("/api/reservations/{reservation_id}", api_reservations_delete)
    app.router.add_get("/api/recordings", api_recordings_get)
    app.router.add_patch("/api/recordings/{recording_id}/metadata", api_recordings_patch_metadata)
    app.router.add_get("/api/recordings/{recording_id}/download", api_recordings_download)
    app.router.add_post("/api/recordings/bulk-download", api_recordings_bulk_download)
    app.router.add_delete("/api/recordings/{recording_id}", api_recordings_delete)

    recorder = RecorderService(app)
    app["recorder"] = recorder

    async def on_startup(_: web.Application) -> None:
        await recorder.start()

    async def on_cleanup(_: web.Application) -> None:
        await recorder.stop()
        await session.close()
        await db.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NHK radio recorder backend server")
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind the web server (default: 8080)",
    )
    parser.add_argument(
        "--debug-log",
        action="store_true",
        help="Enable verbose debug logging for NHK fetch paths and /api/events",
    )
    args = parser.parse_args()

    DEBUG_LOG = args.debug_log
    web.run_app(create_app(), host="0.0.0.0", port=args.port)
