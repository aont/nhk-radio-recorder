from __future__ import annotations

import asyncio
import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from ytmusicapi import YTMusic
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
YOUTUBE_ACCOUNTS_FILE = DATA_DIR / "youtube_accounts.json"
DEFAULT_RECORDING_ALBUM = "NHK Radio Recordings"

SERIES_URL_TMPL = "https://www.nhk.or.jp/radio-api/app/v1/web/series?kana={kana}"
SERIES_KANA_LIST = ("a", "k", "s", "t", "n", "h", "m", "y", "r", "w")
EVENT_URL_TMPL = "https://api.nhk.jp/r7/f/broadcastevent/rs/{series_key}.json?offset=0&size=10&to={to_time}&status=scheduled"
EVENT_LOOKAHEAD_DAYS = 7
CONFIG_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"
SERIES_CACHE_TTL = timedelta(hours=1)
SERIES_WATCH_EXPAND_INTERVAL_SECONDS = 5 * 60
RECORDING_END_DELAY_SECONDS = 60

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nhk-recorder")
DEBUG_LOG = False
DEBUG_NHK_JSON_LOG = False


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return response


@web.middleware
async def frontend_files_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.method not in {"GET", "HEAD"}:
        return await handler(request)

    frontend_root = FRONTEND_DIR.resolve()
    requested_path = request.path.lstrip("/") or "index.html"
    candidate_path = (frontend_root / requested_path).resolve()

    if candidate_path.is_relative_to(frontend_root) and candidate_path.is_file():
        return web.FileResponse(candidate_path)

    return await handler(request)


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
    youtube_uploads: list[dict[str, Any]]


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


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


async def migrate_json_to_sqlite(db: aiosqlite.Connection) -> None:
    legacy_sources = (
        ("reservations", RESERVATIONS_FILE, []),
        ("recordings", RECORDINGS_FILE, []),
        ("series_cache", SERIES_CACHE_FILE, {"value": None, "expires_at": datetime.fromtimestamp(0, timezone.utc).isoformat()}),
        ("youtube_accounts", YOUTUBE_ACCOUNTS_FILE, []),
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


async def read_youtube_accounts(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    payload = await _db_get_json(db, "youtube_accounts", [])
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for account in payload:
        if not isinstance(account, dict):
            continue
        headers = account.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        normalized_headers = {str(k): str(v) for k, v in headers.items() if str(k).strip() and str(v).strip()}
        out.append(
            {
                "id": str(account.get("id") or ""),
                "alias": str(account.get("alias") or ""),
                "headers": normalized_headers,
                "created_at": str(account.get("created_at") or ""),
            }
        )
    return [x for x in out if x["id"] and x["alias"]]


async def write_youtube_accounts(db: aiosqlite.Connection, accounts: list[dict[str, Any]]) -> None:
    await _db_set_json(db, "youtube_accounts", accounts)


async def get_youtube_account_by_id(db: aiosqlite.Connection, account_id: str) -> dict[str, Any] | None:
    for account in await read_youtube_accounts(db):
        if account.get("id") == account_id:
            return account
    return None


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
                if DEBUG_NHK_JSON_LOG:
                    logger.info("[debug] GET JSON: %s (attempt=%d)", url, i + 1)
                async with self.session.get(url, headers=headers) as res:
                    if res.status >= 500 and i < 2:
                        await asyncio.sleep(retries[i])
                        continue
                    payload = await res.json(content_type=None)
                    if DEBUG_NHK_JSON_LOG:
                        logger.info(
                            "[debug] GET JSON done: status=%s keys=%s",
                            res.status,
                            sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                        )
                    return res.status, payload
            except Exception:
                if DEBUG_NHK_JSON_LOG:
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
                    "seriesTitle": part_of_series.get("name") or part_of_series.get("headline") or None,
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

    async def fetch_episode(self, episode_api_url: str) -> dict[str, Any] | None:
        if not episode_api_url:
            return None
        status, payload = await self._get_json(episode_api_url)
        if status == 404 or not isinstance(payload, dict):
            return None
        result = payload.get("result")
        if isinstance(result, list) and result:
            result = result[0]
        if not isinstance(result, dict):
            return None
        return result

    async def enrich_event_with_episode(self, event: dict[str, Any]) -> dict[str, Any]:
        episode_api_url = str(event.get("episodeApiUrl") or "")
        if not episode_api_url:
            return event
        try:
            episode = await self.fetch_episode(episode_api_url)
        except Exception:
            logger.warning("episode fetch failed: %s", episode_api_url)
            return event
        if not episode:
            return event

        enriched = dict(event)
        if episode.get("name"):
            enriched["name"] = episode["name"]
        if episode.get("description"):
            enriched["description"] = episode["description"]
        if isinstance(episode.get("detailedDescription"), dict):
            merged_dd = dict(enriched.get("detailedDescription") or {})
            for key, value in episode["detailedDescription"].items():
                if str(value).strip():
                    merged_dd[key] = str(value).strip()
            enriched["detailedDescription"] = merged_dd
        misc = episode.get("misc") or {}
        if isinstance(misc, dict) and isinstance(misc.get("musicList"), list):
            enriched["musicList"] = misc["musicList"]
        if episode.get("startDate"):
            enriched["startDate"] = episode["startDate"]
        if episode.get("endDate"):
            enriched["endDate"] = episode["endDate"]
        episode_about = episode.get("about") or {}
        episode_series = episode_about.get("partOfSeries") or {}
        if episode_series.get("name"):
            enriched["seriesTitle"] = episode_series["name"]
        return enriched

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


class YouTubeMusicUploader:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytm-upload")

    async def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def upload_recording_for_all_accounts(self, app: web.Application, rec_id: str) -> None:
        accounts = await read_youtube_accounts(app["db"])
        if DEBUG_LOG:
            logger.info(
                "[debug] auto youtube upload trigger: rec_id=%s configured_accounts=%d",
                rec_id,
                len(accounts),
            )
        if not accounts:
            logger.info("youtube upload skipped: no configured accounts")
            return

        await self.upload_recording_for_accounts(app, rec_id, accounts)

    async def upload_recording_for_accounts(
        self,
        app: web.Application,
        rec_id: str,
        accounts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rec = await _recording_by_id(app["db"], rec_id)
        if not rec:
            if DEBUG_LOG:
                logger.info("[debug] youtube upload skipped: recording not found rec_id=%s", rec_id)
            return []

        if DEBUG_LOG:
            logger.info(
                "[debug] youtube upload queue start: rec_id=%s accounts=%s",
                rec_id,
                [a.get("id") for a in accounts],
            )
        results: list[dict[str, Any]] = []
        for account in accounts:
            if DEBUG_LOG:
                logger.info(
                    "[debug] youtube upload account begin: rec_id=%s account_id=%s alias=%s",
                    rec_id,
                    account.get("id"),
                    account.get("alias"),
                )
            result = await self._upload_for_account(rec, account)
            await self._append_upload_result(app["db"], rec_id, result)
            if DEBUG_LOG:
                logger.info(
                    "[debug] youtube upload account end: rec_id=%s account_id=%s status=%s upload_status=%s",
                    rec_id,
                    account.get("id"),
                    result.get("status"),
                    result.get("upload_status"),
                )
            results.append(result)
        if DEBUG_LOG:
            uploaded = sum(1 for row in results if row.get("status") == "uploaded")
            logger.info(
                "[debug] youtube upload queue finished: rec_id=%s uploaded=%d failed_or_other=%d",
                rec_id,
                uploaded,
                len(results) - uploaded,
            )
        return results

    async def upload_recordings_for_account(
        self,
        app: web.Application,
        recording_ids: list[str],
        account: dict[str, Any],
    ) -> list[dict[str, Any]]:
        logger.info(
            "manual youtube bulk upload started: account_id=%s alias=%s total_recordings=%d",
            account.get("id"),
            account.get("alias"),
            len(recording_ids),
        )
        rows: list[dict[str, Any]] = []
        for index, rec_id in enumerate(recording_ids, start=1):
            logger.info(
                "manual youtube upload progress: account_id=%s alias=%s recording=%d/%d rec_id=%s",
                account.get("id"),
                account.get("alias"),
                index,
                len(recording_ids),
                rec_id,
            )
            rec = await _recording_by_id(app["db"], rec_id)
            if not rec:
                logger.warning(
                    "manual youtube upload skipped: rec_id not found account_id=%s rec_id=%s",
                    account.get("id"),
                    rec_id,
                )
                rows.append(
                    {
                        "recording_id": rec_id,
                        "status": "not_found",
                        "account_id": account.get("id"),
                        "alias": account.get("alias"),
                    }
                )
                continue
            results = await self.upload_recording_for_accounts(app, rec_id, [account])
            row = results[0] if results else {"status": "failed", "detail": "upload did not run"}
            row["recording_id"] = rec_id
            logger.info(
                "manual youtube upload result: account_id=%s rec_id=%s status=%s upload_status=%s",
                account.get("id"),
                rec_id,
                row.get("status"),
                row.get("upload_status"),
            )
            rows.append(row)
        success_count = sum(1 for row in rows if row.get("status") == "uploaded")
        logger.info(
            "manual youtube bulk upload finished: account_id=%s alias=%s uploaded=%d failed_or_other=%d",
            account.get("id"),
            account.get("alias"),
            success_count,
            len(rows) - success_count,
        )
        return rows

    async def _upload_for_account(self, rec: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        rec_dir = RECORDINGS_DIR / rec["id"]
        m4a = rec_dir / "download.m4a"
        if not m4a.exists():
            logger.info(
                "youtube upload preparing audio: rec_id=%s account_id=%s action=convert_to_m4a",
                rec.get("id"),
                account.get("id"),
            )
            m4a = await _convert_to_m4a(rec)
            if DEBUG_LOG:
                logger.info(
                    "[debug] youtube upload converted audio: rec_id=%s account_id=%s file=%s",
                    rec.get("id"),
                    account.get("id"),
                    m4a,
                )
        elif DEBUG_LOG:
            logger.info(
                "[debug] youtube upload using existing audio: rec_id=%s account_id=%s file=%s size_bytes=%s",
                rec.get("id"),
                account.get("id"),
                m4a,
                m4a.stat().st_size,
            )
        logger.info(
            "youtube upload start: rec_id=%s account_id=%s alias=%s file=%s",
            rec.get("id"),
            account.get("id"),
            account.get("alias"),
            m4a,
        )

        def run_upload() -> dict[str, Any]:
            auth_file = rec_dir / f"yt_headers_{account['id']}.json"
            auth_file.write_text(json.dumps(account["headers"], ensure_ascii=False, indent=2), encoding="utf-8")
            ytmusic = YTMusic(str(auth_file))
            response = ytmusic.upload_song(str(m4a))
            return {"response": response}

        try:
            if DEBUG_LOG:
                logger.info(
                    "[debug] youtube upload executor submit: rec_id=%s account_id=%s",
                    rec.get("id"),
                    account.get("id"),
                )
            payload = await asyncio.get_running_loop().run_in_executor(self.executor, run_upload)
            raw_response = payload.get("response")
            upload_ok, upload_status = self._interpret_upload_response(raw_response)
            if DEBUG_LOG:
                logger.info(
                    "[debug] youtube upload raw response: rec_id=%s account_id=%s type=%s response=%s",
                    rec.get("id"),
                    account.get("id"),
                    type(raw_response).__name__,
                    to_jsonable(raw_response),
                )
            return {
                "account_id": account["id"],
                "alias": account["alias"],
                "status": "uploaded" if upload_ok else "failed",
                "uploaded_at": utc_now().isoformat(),
                "detail": to_jsonable(raw_response),
                "upload_status": upload_status,
            }
        except Exception as exc:
            logger.exception("youtube upload failed: rec_id=%s account=%s", rec.get("id"), account.get("alias"))
            return {
                "account_id": account["id"],
                "alias": account["alias"],
                "status": "failed",
                "uploaded_at": utc_now().isoformat(),
                "detail": str(exc),
            }

    @staticmethod
    def _interpret_upload_response(response: Any) -> tuple[bool, str]:
        status = str(response)
        if status == "STATUS_SUCCEEDED":
            return True, status
        if status.startswith("STATUS_"):
            return False, status

        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return 200 <= status_code < 300, f"HTTP_{status_code}"

        return True, status

    async def _append_upload_result(self, db: aiosqlite.Connection, rec_id: str, result: dict[str, Any]) -> None:
        async with RECORDINGS_LOCK:
            recordings = await read_json(db, RECORDINGS_FILE)
            for rec in recordings:
                if rec.get("id") != rec_id:
                    continue
                uploads = rec.setdefault("youtube_uploads", [])
                if isinstance(uploads, list):
                    uploads.append(result)
                    if DEBUG_LOG:
                        logger.info(
                            "[debug] youtube upload result appended: rec_id=%s upload_entries=%d last_status=%s",
                            rec_id,
                            len(uploads),
                            result.get("status"),
                        )
            await write_json(db, RECORDINGS_FILE, recordings)


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
        cancellation_tasks: list[asyncio.Task[Any]] = []
        for r in reservations:
            if r["type"] != "series_watch" or r["status"] != "pending":
                continue
            payload = r["payload"]
            seen = set(payload.setdefault("seen_broadcast_event_ids", []))
            series_key = str(payload.get("series_code") or payload["series_id"])
            events = await self.app["nhk"].fetch_events(series_key)
            enriched_events: list[dict[str, Any]] = []
            for ev in events:
                enriched_events.append(await self.app["nhk"].enrich_event_with_episode(ev))

            active_events_by_id = {
                ev.get("broadcastEventId"): ev
                for ev in enriched_events
                if ev.get("broadcastEventId")
            }

            linked_reservations = [
                x
                for x in reservations
                if x["type"] == "single_event"
                and x.get("payload", {}).get("from_series_watch") == r["id"]
                and x["status"] in {"pending", "scheduled"}
            ]

            for linked in linked_reservations:
                linked_event = linked.get("payload", {}).get("event", {})
                linked_beid = linked_event.get("broadcastEventId")
                if not linked_beid:
                    continue
                current_event = active_events_by_id.get(linked_beid)
                if current_event is None:
                    linked["status"] = "cancelled"
                    changed = True
                    task = self.active_recording_tasks.pop(linked["id"], None)
                    if task:
                        task.cancel()
                        cancellation_tasks.append(task)
                    continue
                if linked_event != current_event:
                    linked["payload"]["event"] = current_event
                    linked["payload"]["metadata"] = build_reservation_metadata(
                        payload.get("series_id"),
                        payload.get("series_code"),
                        current_event,
                        payload.get("series_title"),
                    )
                    changed = True
                    task = self.active_recording_tasks.pop(linked["id"], None)
                    if task:
                        task.cancel()
                        cancellation_tasks.append(task)

            for ev in enriched_events:
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
                                "metadata": build_reservation_metadata(
                                    payload["series_id"],
                                    payload.get("series_code"),
                                    ev,
                                    payload.get("series_title"),
                                ),
                            },
                        )
                    )
                )
                seen.add(beid)
                changed = True
            payload["seen_broadcast_event_ids"] = sorted(seen)
        if changed:
            await write_json(self.app["db"], RESERVATIONS_FILE, reservations)
        if cancellation_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*cancellation_tasks)

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
        await self._mark_reservation(reservation["id"], "recording")
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
        stop_dt = end_dt + timedelta(seconds=RECORDING_END_DELAY_SECONDS)

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
        if stop_dt > utc_now():
            await wait_until(stop_dt)

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

        reservation_metadata = reservation["payload"].get("metadata") or {}
        metadata = build_metadata_tags(event, reservation_metadata)
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
                        youtube_uploads=[],
                    )
                )
            )
            await write_json(self.app["db"], RECORDINGS_FILE, recordings)
        self._write_recording_debug_state(rec_dir, "index_written", {"recordings_count": len(recordings)})
        await self._mark_reservation(reservation["id"], "done")
        self._write_recording_debug_state(rec_dir, "reservation_done", {"reservation_id": reservation["id"]})
        logger.info("recording completed: reservation_id=%s rec_id=%s", reservation["id"], rec_id)
        self._write_recording_debug_state(
            rec_dir,
            "youtube_upload_started",
            {"reservation_id": reservation["id"], "rec_id": rec_id},
        )
        if DEBUG_LOG:
            logger.info(
                "[debug] recording post-process: auto upload start reservation_id=%s rec_id=%s",
                reservation["id"],
                rec_id,
            )
        await self.app["youtube_uploader"].upload_recording_for_all_accounts(self.app, rec_id)
        self._write_recording_debug_state(
            rec_dir,
            "youtube_upload_finished",
            {"reservation_id": reservation["id"], "rec_id": rec_id},
        )
        if DEBUG_LOG:
            logger.info(
                "[debug] recording post-process: auto upload finished reservation_id=%s rec_id=%s",
                reservation["id"],
                rec_id,
            )

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


def build_metadata_tags(event: dict[str, Any], reservation_metadata: dict[str, str] | None = None) -> dict[str, str]:
    dd = event.get("detailedDescription") or {}
    description = dd.get("epg80") or dd.get("epg40") or event.get("description") or ""
    reservation_metadata = reservation_metadata or {}
    series_title = str(
        reservation_metadata.get("series_title")
        or event.get("seriesTitle")
        or ""
    ).strip()
    tags = {
        "title": event.get("name") or "Untitled",
        "description": description,
        "album": DEFAULT_RECORDING_ALBUM,
    }
    if series_title:
        tags["album"] = series_title
        tags["series_title"] = series_title
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


def build_reservation_metadata(
    series_id: Any,
    series_code: Any,
    event: dict[str, Any],
    series_title: Any = None,
) -> dict[str, str]:
    resolved_series_title = str(series_title or event.get("seriesTitle") or "").strip()
    return {
        "series_id": str(series_id or ""),
        "series_code": str(series_code or ""),
        "series_title": resolved_series_title,
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
                "[debug] /events: series_key=%s lookahead_days=%s -> %d rows",
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


async def _create_reservation(request: web.Request, reservation_type: str, reservation_payload: dict[str, Any]) -> web.Response:
    if reservation_type == "single_event":
        reservation_payload["metadata"] = build_reservation_metadata(
            reservation_payload.get("series_id"),
            reservation_payload.get("series_code"),
            reservation_payload.get("event") or {},
            reservation_payload.get("series_title"),
        )
    if reservation_type == "series_watch":
        reservation_payload["metadata"] = build_series_watch_metadata(
            reservation_payload.get("series_id"),
            reservation_payload.get("series_code"),
            reservation_payload,
        )
    reservation = Reservation(
        id=str(uuid.uuid4()),
        type=reservation_type,
        created_at=utc_now().isoformat(),
        status="pending",
        payload=reservation_payload,
    )
    reservations = await read_json(request.app["db"], RESERVATIONS_FILE)
    reservations.append(asdict(reservation))
    await write_json(request.app["db"], RESERVATIONS_FILE, reservations)

    if reservation_type == "series_watch":
        recorder = request.app.get("recorder")
        if recorder:
            await recorder._expand_series_watchers()

    return web.json_response(asdict(reservation))


async def reservations_post_single_event(request: web.Request) -> web.Response:
    payload = await request.json()
    return await _create_reservation(request, "single_event", payload)


async def reservations_post_watch_series(request: web.Request) -> web.Response:
    payload = await request.json()
    return await _create_reservation(request, "series_watch", payload)


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
    metadata = {k: str(v) for k, v in (rec.get("metadata") or {}).items()}
    if not metadata.get("album", "").strip():
        metadata["album"] = DEFAULT_RECORDING_ALBUM
    for k, v in metadata.items():
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


async def api_recordings_bulk_upload(request: web.Request) -> web.Response:
    payload = await request.json()
    ids = payload.get("ids", [])
    account_id = str(payload.get("account_id") or "").strip()
    logger.info(
        "bulk upload request received: account_id=%s ids_count=%s",
        account_id or "(missing)",
        len(ids) if isinstance(ids, list) else "(invalid)",
    )
    if not isinstance(ids, list) or not ids:
        raise web.HTTPBadRequest(text="ids must be a non-empty array")
    if not account_id:
        raise web.HTTPBadRequest(text="account_id is required")

    normalized_ids = [str(rec_id).strip() for rec_id in ids if str(rec_id).strip()]
    if not normalized_ids:
        raise web.HTTPBadRequest(text="ids must include non-empty recording ids")

    account = await get_youtube_account_by_id(request.app["db"], account_id)
    if not account:
        raise web.HTTPNotFound(text="youtube account not found")

    results = await request.app["youtube_uploader"].upload_recordings_for_account(
        request.app,
        normalized_ids,
        account,
    )
    uploaded_count = sum(1 for row in results if row.get("status") == "uploaded")
    logger.info(
        "bulk upload request completed: account_id=%s requested=%d uploaded=%d",
        account_id,
        len(normalized_ids),
        uploaded_count,
    )
    return web.json_response({"ok": True, "results": results})


async def api_recordings_delete(request: web.Request) -> web.Response:
    rec_id = request.match_info["recording_id"]
    rec_dir = RECORDINGS_DIR / rec_id
    shutil.rmtree(rec_dir, ignore_errors=True)
    async with RECORDINGS_LOCK:
        recordings = [r for r in await read_json(request.app["db"], RECORDINGS_FILE) if r["id"] != rec_id]
        await write_json(request.app["db"], RECORDINGS_FILE, recordings)
    return web.json_response({"ok": True})


async def api_youtube_accounts_get(request: web.Request) -> web.Response:
    accounts = await read_youtube_accounts(request.app["db"])
    sanitized = [{k: v for k, v in a.items() if k != "headers"} for a in accounts]
    return web.json_response(sanitized)


async def api_youtube_accounts_post(request: web.Request) -> web.Response:
    payload = await request.json()
    alias = str(payload.get("alias") or "").strip()
    headers = payload.get("headers")
    if not alias:
        raise web.HTTPBadRequest(text="alias is required")
    if not isinstance(headers, dict) or not headers:
        raise web.HTTPBadRequest(text="headers must be a non-empty object")
    normalized_headers = {str(k).strip(): str(v).strip() for k, v in headers.items() if str(k).strip() and str(v).strip()}
    if not normalized_headers:
        raise web.HTTPBadRequest(text="headers must include at least one non-empty key/value")

    accounts = await read_youtube_accounts(request.app["db"])
    account = {
        "id": str(uuid.uuid4()),
        "alias": alias,
        "headers": normalized_headers,
        "created_at": utc_now().isoformat(),
    }
    accounts.append(account)
    await write_youtube_accounts(request.app["db"], accounts)
    return web.json_response({k: v for k, v in account.items() if k != "headers"})


async def api_youtube_accounts_delete(request: web.Request) -> web.Response:
    account_id = request.match_info["account_id"]
    accounts = await read_youtube_accounts(request.app["db"])
    accounts = [a for a in accounts if a.get("id") != account_id]
    await write_youtube_accounts(request.app["db"], accounts)
    return web.json_response({"ok": True})


async def create_app() -> web.Application:
    ensure_dirs()
    timeout = ClientTimeout(total=10)
    session = ClientSession(timeout=timeout)
    db = await aiosqlite.connect(DATABASE_FILE)
    await init_db(db)
    await migrate_json_to_sqlite(db)

    app = web.Application(middlewares=[cors_middleware, frontend_files_middleware])
    app["session"] = session
    app["db"] = db
    app["nhk"] = NHKClient(session)
    app["series_cache"] = await load_series_cache(db)
    app["youtube_uploader"] = YouTubeMusicUploader()

    app.router.add_get("/series", api_series)
    app.router.add_get("/series/resolve", api_series_resolve)
    app.router.add_get("/events", api_events)
    app.router.add_get("/reservations", api_reservations_get)
    app.router.add_post("/reservation/single-event", reservations_post_single_event)
    app.router.add_post("/reservation/watch-series", reservations_post_watch_series)
    app.router.add_delete("/reservations/{reservation_id}", api_reservations_delete)
    app.router.add_get("/youtube-accounts", api_youtube_accounts_get)
    app.router.add_post("/youtube-accounts", api_youtube_accounts_post)
    app.router.add_delete("/youtube-accounts/{account_id}", api_youtube_accounts_delete)
    app.router.add_get("/recordings", api_recordings_get)
    app.router.add_patch("/recordings/{recording_id}/metadata", api_recordings_patch_metadata)
    app.router.add_get("/recordings/{recording_id}/download", api_recordings_download)
    app.router.add_post("/recordings/bulk-download", api_recordings_bulk_download)
    app.router.add_post("/recordings/bulk-upload", api_recordings_bulk_upload)
    app.router.add_delete("/recordings/{recording_id}", api_recordings_delete)
    app.router.add_static("/recordings", RECORDINGS_DIR)

    recorder = RecorderService(app)
    app["recorder"] = recorder

    async def on_startup(_: web.Application) -> None:
        await recorder.start()

    async def on_cleanup(_: web.Application) -> None:
        await recorder.stop()
        await app["youtube_uploader"].close()
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
        help="Enable verbose debug logging for NHK fetch paths, /events, and YouTube auto upload flow",
    )
    parser.add_argument(
        "--debug-nhk-json-log",
        action="store_true",
        help="Enable verbose debug logging only for NHK API JSON fetches",
    )
    args = parser.parse_args()

    DEBUG_LOG = args.debug_log
    DEBUG_NHK_JSON_LOG = args.debug_nhk_json_log or os.getenv("NHK_DEBUG_JSON_LOG", "").lower() in {"1", "true", "yes", "on"}
    web.run_app(create_app(), host="0.0.0.0", port=args.port)
