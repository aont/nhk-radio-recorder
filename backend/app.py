from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from aiohttp import ClientSession, ClientTimeout, web

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RECORDINGS_DIR = BASE_DIR / "recordings"
FRONTEND_DIR = BASE_DIR / "frontend"
RESERVATIONS_FILE = DATA_DIR / "reservations.json"
RECORDINGS_FILE = DATA_DIR / "recordings.json"

SERIES_URL_TMPL = "https://www.nhk.or.jp/radio-api/app/v1/web/series?kana={kana}"
SERIES_KANA_LIST = ("a", "k", "s", "t", "n", "h", "m", "y", "r", "w")
EVENT_URL_TMPL = "https://api.nhk.jp/r7/f/broadcastevent/rs/{series_id}.json?to={to_time}&status=scheduled"
CONFIG_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nhk-recorder")


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
    if not RESERVATIONS_FILE.exists():
        RESERVATIONS_FILE.write_text("[]", encoding="utf-8")
    if not RECORDINGS_FILE.exists():
        RECORDINGS_FILE.write_text("[]", encoding="utf-8")


def read_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class NHKClient:
    def __init__(self, session: ClientSession):
        self.session = session

    async def _get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        retries = [0.5, 1.5]
        for i in range(3):
            try:
                async with self.session.get(url, headers=headers) as res:
                    if res.status >= 500 and i < 2:
                        await asyncio.sleep(retries[i])
                        continue
                    return res.status, await res.json(content_type=None)
            except Exception:
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
                out.append(
                    {
                        "id": series_id,
                        "title": str(item["title"]).strip(),
                        "broadcasts": broadcasts,
                        "url": str(item["url"]).strip(),
                        "thumbnailUrl": (item.get("thumbnail_url") or "").strip() or None,
                        "scheduleText": (item.get("schedule") or "").strip() or None,
                        "areaName": (item.get("area") or "").strip() or None,
                    }
                )
        return out

    async def fetch_events(self, series_id: int, to_days: int = 1) -> list[dict[str, Any]]:
        to_time = (datetime.now() + timedelta(days=to_days)).strftime("%Y-%m-%dT%H:%M")
        status, payload = await self._get_json(EVENT_URL_TMPL.format(series_id=series_id, to_time=to_time))
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
            out.append(
                {
                    "name": ev.get("name", "Untitled"),
                    "description": ev.get("description"),
                    "startDate": start_dt.isoformat(),
                    "endDate": end_dt.isoformat(),
                    "broadcastEventId": ig.get("broadcastEventId"),
                    "serviceId": ig.get("serviceId"),
                    "areaId": ig.get("areaId"),
                    "detailedDescription": dd,
                    "musicList": ((ev.get("misc") or {}).get("musicList") or []),
                }
            )
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

    async def start(self) -> None:
        self.loop_task = asyncio.create_task(self.scheduler_loop())

    async def stop(self) -> None:
        if self.loop_task:
            self.loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.loop_task

    async def scheduler_loop(self) -> None:
        while True:
            try:
                await self._expand_series_watchers()
                await self._run_due_recordings()
            except Exception as exc:
                logger.exception("Scheduler error: %s", exc)
            await asyncio.sleep(30)

    async def _expand_series_watchers(self) -> None:
        reservations = read_json(RESERVATIONS_FILE)
        changed = False
        for r in reservations:
            if r["type"] != "series_watch" or r["status"] != "pending":
                continue
            payload = r["payload"]
            seen = set(payload.setdefault("seen_broadcast_event_ids", []))
            events = await self.app["nhk"].fetch_events(int(payload["series_id"]))
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
                            payload={"series_id": payload["series_id"], "event": ev, "from_series_watch": r["id"]},
                        )
                    )
                )
                seen.add(beid)
                changed = True
            payload["seen_broadcast_event_ids"] = sorted(seen)
        if changed:
            write_json(RESERVATIONS_FILE, reservations)

    async def _run_due_recordings(self) -> None:
        reservations = read_json(RESERVATIONS_FILE)
        now = utc_now()
        changed = False
        for r in reservations:
            if r["type"] != "single_event" or r["status"] != "pending":
                continue
            event = r["payload"]["event"]
            start_dt = datetime.fromisoformat(event["startDate"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if start_dt > now:
                continue
            r["status"] = "scheduled"
            changed = True
            asyncio.create_task(self.execute_recording(r))
        if changed:
            write_json(RESERVATIONS_FILE, reservations)

    async def execute_recording(self, reservation: dict[str, Any]) -> None:
        event = reservation["payload"]["event"]
        service_id = event["serviceId"]
        stream_key = "fm" if service_id == "r3" else service_id
        catalogs = await self.app["nhk"].fetch_stream_catalog()
        catalog = catalogs.get(event["areaId"])
        if not catalog:
            await self._mark_reservation(reservation["id"], "failed")
            return
        stream_url = catalog["streams"].get(stream_key)
        if not stream_url:
            await self._mark_reservation(reservation["id"], "failed")
            return

        rec_id = str(uuid.uuid4())
        rec_dir = RECORDINGS_DIR / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        manifest = rec_dir / "recording.m3u8"

        start_dt = datetime.fromisoformat(event["startDate"])
        end_dt = datetime.fromisoformat(event["endDate"])
        duration = max(1, int((end_dt - start_dt).total_seconds()))

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            stream_url,
            "-t",
            str(duration),
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
        proc = await asyncio.create_subprocess_exec(*cmd)
        ret = await proc.wait()

        if ret != 0:
            shutil.rmtree(rec_dir, ignore_errors=True)
            await self._mark_reservation(reservation["id"], "failed")
            return

        metadata = build_metadata_tags(event)
        recordings = read_json(RECORDINGS_FILE)
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
        write_json(RECORDINGS_FILE, recordings)
        await self._mark_reservation(reservation["id"], "done")

    async def _mark_reservation(self, reservation_id: str, status: str) -> None:
        reservations = read_json(RESERVATIONS_FILE)
        for r in reservations:
            if r["id"] == reservation_id:
                r["status"] = status
        write_json(RESERVATIONS_FILE, reservations)


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


async def api_series(request: web.Request) -> web.Response:
    cache = request.app["series_cache"]
    now = utc_now()
    if cache["value"] and cache["expires_at"] > now:
        return web.json_response(cache["value"])
    try:
        data = await request.app["nhk"].fetch_series()
        cache["value"] = data
        cache["expires_at"] = now + timedelta(hours=6)
        return web.json_response(data)
    except Exception as exc:
        logger.warning("series fetch failed: %s", exc)
        if cache["value"] is not None:
            return web.json_response(cache["value"])
        return web.json_response([])


async def api_events(request: web.Request) -> web.Response:
    sid = int(request.query["series_id"])
    to_days = int(request.query.get("to_days", "1"))
    try:
        return web.json_response(await request.app["nhk"].fetch_events(sid, to_days))
    except Exception as exc:
        logger.warning("event fetch failed: %s", exc)
        return web.json_response([])


async def api_reservations_get(request: web.Request) -> web.Response:
    return web.json_response(read_json(RESERVATIONS_FILE))


async def api_reservations_post(request: web.Request) -> web.Response:
    payload = await request.json()
    reservation = Reservation(
        id=str(uuid.uuid4()),
        type=payload["type"],
        created_at=utc_now().isoformat(),
        status="pending",
        payload=payload["payload"],
    )
    reservations = read_json(RESERVATIONS_FILE)
    reservations.append(asdict(reservation))
    write_json(RESERVATIONS_FILE, reservations)
    return web.json_response(asdict(reservation))


async def api_reservations_delete(request: web.Request) -> web.Response:
    rid = request.match_info["reservation_id"]
    reservations = [r for r in read_json(RESERVATIONS_FILE) if r["id"] != rid]
    write_json(RESERVATIONS_FILE, reservations)
    return web.json_response({"ok": True})


async def api_recordings_get(request: web.Request) -> web.Response:
    return web.json_response(read_json(RECORDINGS_FILE))


def _recording_by_id(rec_id: str) -> dict[str, Any] | None:
    for rec in read_json(RECORDINGS_FILE):
        if rec["id"] == rec_id:
            return rec
    return None


async def api_recordings_patch_metadata(request: web.Request) -> web.Response:
    rec_id = request.match_info["recording_id"]
    payload = await request.json()
    recordings = read_json(RECORDINGS_FILE)
    for rec in recordings:
        if rec["id"] == rec_id:
            rec["metadata"].update({k: str(v) for k, v in payload.items()})
    write_json(RECORDINGS_FILE, recordings)
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
    rec = _recording_by_id(rec_id)
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
            rec = _recording_by_id(rec_id)
            if not rec:
                continue
            m4a = await _convert_to_m4a(rec)
            zf.write(m4a, arcname=f"{rec_id}.m4a")
    return web.FileResponse(zippath, headers={"Content-Disposition": 'attachment; filename="recordings.zip"'})


async def api_recordings_delete(request: web.Request) -> web.Response:
    rec_id = request.match_info["recording_id"]
    rec_dir = RECORDINGS_DIR / rec_id
    shutil.rmtree(rec_dir, ignore_errors=True)
    recordings = [r for r in read_json(RECORDINGS_FILE) if r["id"] != rec_id]
    write_json(RECORDINGS_FILE, recordings)
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(FRONTEND_DIR / "index.html")


async def create_app() -> web.Application:
    ensure_dirs()
    timeout = ClientTimeout(total=10)
    session = ClientSession(timeout=timeout)
    app = web.Application()
    app["session"] = session
    app["nhk"] = NHKClient(session)
    app["series_cache"] = {"value": None, "expires_at": datetime.fromtimestamp(0, timezone.utc)}

    app.router.add_get("/", index)
    app.router.add_static("/static", FRONTEND_DIR)
    app.router.add_static("/recordings", RECORDINGS_DIR)

    app.router.add_get("/api/series", api_series)
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

    async def on_startup(_: web.Application) -> None:
        recorder.loop_task = asyncio.create_task(recorder.scheduler_loop())

    async def on_cleanup(_: web.Application) -> None:
        if recorder.loop_task:
            recorder.loop_task.cancel()
            try:
                await recorder.loop_task
            except asyncio.CancelledError:
                pass
        await session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
