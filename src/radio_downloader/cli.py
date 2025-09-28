"""Command line entry point for the radio downloader."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import List

import aiohttp

from .events import fetch_events
from .hls import fetch_hls_map, pick_variant
from .recorder import record_one


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NHKラジオ HLS 録音予約 (asyncio + ffmpeg)")
    parser.add_argument(
        "--event-url",
        action="append",
        required=True,
        help="放送予定（BroadcastEvent）JSONのURL。複数指定可。",
    )
    parser.add_argument(
        "--area",
        default="tokyo",
        help="地域（config_web.xml の <area> 値。例: tokyo/osaka など）",
    )
    parser.add_argument(
        "--service",
        default=None,
        choices=["r1", "r2", "fm"],
        help="サービス（r1/r2/fm）。JSONから判別できない場合に使用。",
    )
    parser.add_argument(
        "--variant",
        default="master",
        choices=["auto", "master", "master48k"],
        help="HLSプレイリストのバリアント選択。既定は master（そのまま）。",
    )
    parser.add_argument("--outdir", default="./recordings", help="保存ディレクトリ")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 実行ファイルのパス")
    parser.add_argument("--prepad", type=int, default=5, help="開始前の余裕秒")
    parser.add_argument("--postpad", type=int, default=30, help="終了後の余裕秒")
    parser.add_argument("--loglevel", default="error", help="ffmpeg の -loglevel（例: error, warning, info）")
    parser.add_argument("--dry-run", action="store_true", help="録音せず予約内容を表示")
    return parser


async def run_async(args: argparse.Namespace) -> None:
    outdir = Path(args.outdir)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        area_map = await fetch_hls_map(session)

        if args.area not in area_map:
            raise SystemExit(
                "config_web.xml に area='{}' が見つかりません。利用可能: {}".format(
                    args.area, ", ".join(sorted(area_map.keys()))
                )
            )

        tasks: List[asyncio.Task] = []

        for url in args.event_url:
            events = await fetch_events(session, url)

            for event in events:
                service = event.service or args.service or "r2"
                area = (event.area or args.area).lower()

                svc_map = area_map.get(area)
                if not svc_map or service not in svc_map:
                    if service != "r2" and svc_map and "r2" in svc_map:
                        service = "r2"
                    elif svc_map:
                        available = ", ".join(sorted(svc_map.keys()))
                        raise SystemExit(
                            f"area='{area}' に service='{service}' が見つかりません（利用可能: {available}）。"
                        )

                hls = area_map.get(area, {}).get(service)
                if not hls:
                    raise SystemExit(
                        f"HLS URL が取得できませんでした: area={area}, service={service}"
                    )

                hls = pick_variant(hls, args.variant)

                if args.dry_run:
                    print(f"[DRY-RUN] {event.title} ({service}@{area})")
                    print(
                        "  time: {} → {} (dur={}s)".format(
                            event.start.isoformat(),
                            event.end.isoformat(),
                            int(event.duration.total_seconds()),
                        )
                    )
                    print(f"  HLS : {hls}")
                    continue

                task = asyncio.create_task(
                    record_one(
                        event=event,
                        hls_url=hls,
                        outdir=outdir,
                        prepad=args.prepad,
                        postpad=args.postpad,
                        ffmpeg_path=args.ffmpeg,
                        loglevel=args.loglevel,
                    )
                )
                tasks.append(task)

        if args.dry_run:
            return

        if tasks:
            await asyncio.gather(*tasks)
        else:
            print("実行する録音タスクがありません。")


def main(argv: List[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        asyncio.run(run_async(args))
    except KeyboardInterrupt:
        print("Interrupted.")
