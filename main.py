#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiohttp
import xml.etree.ElementTree as ET

# 絶対時刻スリープ（Linux/Windows）。未対応OSやImportError時はフォールバック。
try:
    from sleep_absolute import wait_until as abs_wait_until  # pip install from GitHub
except Exception:
    abs_wait_until = None  # type: ignore


# -----------------------------
# モデル
# -----------------------------

@dataclasses.dataclass
class NHKEvent:
    event_id: str
    title: str
    start: dt.datetime  # timezone-aware
    end: dt.datetime    # timezone-aware
    service: Optional[str] = None  # 'r1' / 'r2' / 'fm'
    area: Optional[str] = None     # 'tokyo', 'osaka', ...

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start

# -----------------------------
# ユーティリティ
# -----------------------------

JP_TZ = ZoneInfo("Asia/Tokyo")

def _parse_iso8601(value: Any, default_tz=JP_TZ) -> Optional[dt.datetime]:
    """ISO8601風の文字列/UNIX秒からdatetimeに変換"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone(default_tz)
    s = str(value).strip()
    if not s:
        return None
    # 末尾 'Z' を +00:00 に
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 小数秒なしのときもある
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        # "YYYYMMDDHHMMSS" の緊急対応
        try:
            d = dt.datetime.strptime(s, "%Y%m%d%H%M%S")
            d = d.replace(tzinfo=default_tz)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=default_tz)
    return d


def _any_key(d: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[Any]:
    for k in keys:
        if k in d:
            return d[k]
    return None


def sanitize_filename(name: str) -> str:
    name = name.strip()
    # ファイル名に使えない文字を除去/置換
    name = re.sub(r'[\\/:*?"<>|\x00-\x1F]', "_", name)
    # 連続アンダースコアを縮約
    name = re.sub(r"_+", "_", name)
    return name[:120] or "untitled"


async def sleep_until(target: dt.datetime) -> None:
    """絶対時刻まで非ブロッキングで待機"""
    now = dt.datetime.now(tz=target.tzinfo or dt.timezone.utc)
    if target <= now:
        return
    if abs_wait_until is not None:
        try:
            await abs_wait_until(target)
            return
        except NotImplementedError:
            pass
        except Exception:
            # 何かあればフォールバック
            pass
    # フォールバック：相対sleep
    await asyncio.sleep((target - dt.datetime.now(tz=target.tzinfo or dt.timezone.utc)).total_seconds())


# -----------------------------
# config_web.xml → HLS URL辞書
# -----------------------------

CONFIG_XML_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"

async def fetch_hls_map(session: aiohttp.ClientSession) -> Dict[str, Dict[str, str]]:
    """
    各エリア(area) → { 'r1': url, 'r2': url, 'fm': url } の辞書を返す
    config_web.xml の <stream_url><data>... を解析する
    """
    headers = {"User-Agent": "nhk-radio-recorder/1.0 (+asyncio)"}
    async with session.get(CONFIG_XML_URL, headers=headers) as resp:
        resp.raise_for_status()
        text = await resp.text()

    root = ET.fromstring(text)
    ns = {}  # 名前空間なし

    area_to_service: Dict[str, Dict[str, str]] = {}

    for data in root.findall(".//stream_url/data", ns):
        area = (data.findtext("area") or "").strip()
        if not area:
            continue
        r1 = (data.findtext("r1hls") or "").strip()
        r2 = (data.findtext("r2hls") or "").strip()
        fm = (data.findtext("fmhls") or "").strip()
        d: Dict[str, str] = {}
        if r1:
            d["r1"] = r1
        if r2:
            d["r2"] = r2
        if fm:
            d["fm"] = fm
        if d:
            area_to_service[area] = d

    if not area_to_service:
        raise RuntimeError("config_web.xml の解析に失敗しました。NHK側の仕様変更の可能性があります。")

    return area_to_service


def pick_variant(url: str, variant: str) -> str:
    """
    master.m3u8 → master48k.m3u8 に差し替える等の簡易処理。
    variant: 'master' | 'master48k' | 'auto'
    """
    if variant == "auto":
        return url
    if url.endswith("master.m3u8") and variant == "master48k":
        return url[:-len("master.m3u8")] + "master48k.m3u8"
    return url


# -----------------------------
# 放送予定JSONの解析（緩く）
# -----------------------------

START_KEYS = ("start_time", "startTime", "startDateTime", "startDate", "start")
END_KEYS   = ("end_time", "endTime", "endDateTime", "endDate", "end")
TITLE_KEYS = ("title", "event_title", "program_title", "name")
SERVICE_KEYS = ("service", "serviceId", "broadcastServiceId", "onair_service", "channel")
AREA_KEYS    = ("area", "areaKey", "areakey", "region", "regionCode")
ID_KEYS      = ("broadcastEventId", "event_id", "id", "be_id", "item_id", "content_id")

def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x)


def extract_events_from_json(payload: Any, default_tz=JP_TZ) -> List[NHKEvent]:
    """
    JSONのどこに置かれていても、「start*」「end*」を両方持つ辞書をイベント候補として抽出。
    先に出てきた順に採用。
    """
    events: List[NHKEvent] = []
    for d in _walk(payload):
        if not isinstance(d, dict):
            continue
        start_raw = _any_key(d, START_KEYS)
        end_raw   = _any_key(d, END_KEYS)
        if start_raw is None or end_raw is None:
            continue
        start = _parse_iso8601(start_raw, default_tz)
        end   = _parse_iso8601(end_raw,   default_tz)
        if not start or not end or end <= start:
            continue
        title = _any_key(d, TITLE_KEYS) or "NHK Radio"
        service = _any_key(d, SERVICE_KEYS)
        if isinstance(service, dict):  # たとえば {"id":"r2"} のようなケース
            service = service.get("id") or service.get("name")
        if isinstance(service, str):
            s = service.lower()
            if "r1" in s: service = "r1"
            elif "r2" in s or "rs" in s: service = "r2"
            elif "fm" in s: service = "fm"
            else: service = None
        else:
            service = None

        area = _any_key(d, AREA_KEYS)
        if isinstance(area, dict):
            area = area.get("id") or area.get("name")
        if isinstance(area, str):
            area = area.lower()
        else:
            area = None

        event_id = _any_key(d, ID_KEYS) or ""
        if isinstance(event_id, dict):
            event_id = event_id.get("id") or ""
        events.append(NHKEvent(
            event_id=str(event_id),
            title=str(title),
            start=start,
            end=end,
            service=service,
            area=area,
        ))
    return events


async def fetch_events(session: aiohttp.ClientSession, url: str) -> List[NHKEvent]:
    headers = {"User-Agent": "nhk-radio-recorder/1.0 (+asyncio)"}
    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        # JSONの content-type が不正なこともあるので content_type=None で受ける
        payload = await resp.json(content_type=None)
    events = extract_events_from_json(payload, default_tz=JP_TZ)
    if not events:
        # デバッグ用に一部を表示
        snippet = json.dumps(payload, ensure_ascii=False)[:500]
        raise RuntimeError(f"放送予定JSONからイベントを抽出できませんでした: {url}\npayload一部: {snippet} ...")
    return events


# -----------------------------
# ffmpeg 実行
# -----------------------------

def build_ffmpeg_cmd(
    ffmpeg: str,
    hls_url: str,
    out_path: Path,
    duration_sec: int,
    copy_mode: bool = True,
    loglevel: str = "error",
) -> List[str]:
    base = [
        ffmpeg,
        "-nostats",
        "-loglevel", loglevel,
        # HLSの再接続関連
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_at_eof", "1",
        "-rw_timeout", "15000000",  # 15秒 (マイクロ秒)
        "-i", hls_url,
        "-vn",
        "-t", str(int(duration_sec)),
        "-y",
    ]
    if copy_mode:
        base += ["-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path)]
    else:
        # フォールバック: MP3に再エンコード
        base += ["-c:a", "libmp3lame", "-q:a", "2", str(out_path.with_suffix(".mp3"))]
    return base


async def run_ffmpeg(cmd: List[str]) -> int:
    proc = await asyncio.create_subprocess_exec(*cmd)
    return await proc.wait()


# -----------------------------
# 録音タスク
# -----------------------------

async def record_one(
    event: NHKEvent,
    hls_url: str,
    outdir: Path,
    prepad: int,
    postpad: int,
    ffmpeg_path: str,
    loglevel: str = "error",
) -> None:
    # 前後余裕を加味
    start_at = event.start - dt.timedelta(seconds=max(0, prepad))
    stop_at  = event.end   + dt.timedelta(seconds=max(0, postpad))
    now = dt.datetime.now(tz=event.start.tzinfo)
    if stop_at <= now:
        print(f"[SKIP] {event.title} は終了済み: stop {stop_at.isoformat()}")
        return

    # 出力ファイル名
    stamp = event.start.astimezone(JP_TZ).strftime("%Y%m%d_%H%M")
    base = f"{stamp}_{sanitize_filename(event.title)}"
    out = outdir / f"{base}.m4a"

    # 待機
    if start_at > now:
        print(f"[WAIT] {event.title} → {start_at.isoformat()} に開始（HLS: {hls_url}）")
        await sleep_until(start_at)
    else:
        print(f"[LATE START] すでに開始時刻を過ぎています。即時開始。 {now.isoformat()} > {start_at.isoformat()}")

    # 残り時間で録音
    now2 = dt.datetime.now(tz=event.start.tzinfo)
    duration = int((stop_at - now2).total_seconds())
    if duration <= 0:
        print(f"[SKIP] 録音時間が0秒以下: {event.title}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)

    # まずは copy で保存
    cmd = build_ffmpeg_cmd(ffmpeg_path, hls_url, out, duration_sec=duration, copy_mode=True, loglevel=loglevel)
    print(f"[FFMPEG] {' '.join(cmd)}")
    rc = await run_ffmpeg(cmd)
    if rc == 0:
        print(f"[DONE] {out}")
        return

    # 失敗したらフォールバックで再エンコード
    print(f"[RETRY] copy保存に失敗したため、libmp3lameで再エンコードします (rc={rc})")
    cmd2 = build_ffmpeg_cmd(ffmpeg_path, hls_url, out, duration_sec=duration, copy_mode=False, loglevel=loglevel)
    print(f"[FFMPEG] {' '.join(cmd2)}")
    rc2 = await run_ffmpeg(cmd2)
    if rc2 == 0:
        print(f"[DONE] {out.with_suffix('.mp3')}")
    else:
        print(f"[FAIL] ffmpeg失敗 rc={rc2}")


# -----------------------------
# メイン
# -----------------------------

async def main():
    p = argparse.ArgumentParser(description="NHKラジオ HLS 録音予約 (asyncio + ffmpeg)")
    p.add_argument("--event-url", action="append", required=True,
                   help="放送予定（BroadcastEvent）JSONのURL。複数指定可。")
    p.add_argument("--area", default="tokyo", help="地域（config_web.xml の <area> 値。例: tokyo/osaka など）")
    p.add_argument("--service", default=None, choices=["r1", "r2", "fm"],
                   help="サービス（r1/r2/fm）。JSONから判別できない場合に使用。")
    p.add_argument("--variant", default="master", choices=["auto", "master", "master48k"],
                   help="HLSプレイリストのバリアント選択。既定は master（そのまま）。")
    p.add_argument("--outdir", default="./recordings", help="保存ディレクトリ")
    p.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 実行ファイルのパス")
    p.add_argument("--prepad", type=int, default=5, help="開始前の余裕秒")
    p.add_argument("--postpad", type=int, default=30, help="終了後の余裕秒")
    p.add_argument("--loglevel", default="error", help="ffmpeg の -loglevel（例: error, warning, info）")
    p.add_argument("--dry-run", action="store_true", help="録音せず予約内容を表示")

    args = p.parse_args()

    outdir = Path(args.outdir)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        # HLS URLマップを取得
        area_map = await fetch_hls_map(session)

        if args.area not in area_map:
            raise SystemExit(f"config_web.xml に area='{args.area}' が見つかりません。利用可能: {', '.join(sorted(area_map.keys()))}")

        tasks: List[asyncio.Task] = []

        for url in args.event_url:
            events = await fetch_events(session, url)

            # 複数イベント対応：JSONの中から抽出できたもの全部を処理
            for ev in events:
                # service/area の補完
                service = ev.service or args.service or "r2"
                area = (ev.area or args.area).lower()

                # HLS URL 決定
                svc_map = area_map.get(area)
                if not svc_map or service not in svc_map:
                    # サービスがそのエリアに存在しない場合、r2は全国共通のことが多いので fallback でr2を試す
                    if service != "r2" and "r2" in (svc_map or {}):
                        service = "r2"
                    elif svc_map:
                        # 何も見つからない
                        avail = ", ".join(sorted(svc_map.keys()))
                        raise SystemExit(f"area='{area}' に service='{service}' が見つかりません（利用可能: {avail}）。")

                hls = area_map.get(area, {}).get(service)
                if not hls:
                    raise SystemExit(f"HLS URL が取得できませんでした: area={area}, service={service}")

                hls = pick_variant(hls, args.variant)

                # dry-run 表示
                if args.dry_run:
                    print(f"[DRY-RUN] {ev.title} ({service}@{area})")
                    print(f"  time: {ev.start.isoformat()} → {ev.end.isoformat()} (dur={int(ev.duration.total_seconds())}s)")
                    print(f"  HLS : {hls}")
                    continue

                # 録音タスク
                t = asyncio.create_task(record_one(
                    event=ev,
                    hls_url=hls,
                    outdir=outdir,
                    prepad=args.prepad,
                    postpad=args.postpad,
                    ffmpeg_path=args.ffmpeg,
                    loglevel=args.loglevel,
                ))
                tasks.append(t)

        if args.dry_run:
            return

        if tasks:
            await asyncio.gather(*tasks)
        else:
            print("実行する録音タスクがありません。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted.")
