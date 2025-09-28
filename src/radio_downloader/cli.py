"""Command line entry point for the radio downloader."""

from __future__ import annotations

import argparse
import asyncio
import datetime as _datetime
from pathlib import Path
from typing import List

import aiohttp

from .events import fetch_events
from .hls import fetch_hls_map, pick_variant
from .recorder import record_one


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NHK Radio HLS recording scheduler (asyncio + ffmpeg)"
    )
    parser.add_argument(
        "--event-url",
        action="extend",
        nargs="+",
        default=[],
        help="BroadcastEvent JSON URL(s). Provide multiple values separated by spaces.",
    )
    parser.add_argument(
        "--series-id",
        action="extend",
        nargs="+",
        default=[],
        help="Series ID (e.g. Z9L1V2M24L). Provide multiple values separated by spaces.",
    )
    parser.add_argument(
        "--area",
        default="tokyo",
        help="Area (the <area> value from config_web.xml, e.g. tokyo/osaka).",
    )
    parser.add_argument(
        "--service",
        default=None,
        choices=["r1", "r2", "fm"],
        help="Service (r1/r2/fm). Use when the JSON does not specify it.",
    )
    parser.add_argument(
        "--variant",
        default="master",
        choices=["auto", "master", "master48k"],
        help="HLS playlist variant. Defaults to master.",
    )
    parser.add_argument("--outdir", default="./recordings", help="Output directory.")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path to the ffmpeg executable.")
    parser.add_argument("--prepad", type=int, default=5, help="Seconds to start recording before the event.")
    parser.add_argument(
        "--postpad",
        type=int,
        default=30,
        help="Seconds to continue recording after the event ends.",
    )
    parser.add_argument(
        "--loglevel",
        default="error",
        help="ffmpeg -loglevel value (e.g. error, warning, info).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the scheduled recordings without invoking ffmpeg.",
    )
    parser.add_argument(
        "--refresh-sec",
        type=int,
        default=300,
        help="Interval (seconds) to refresh broadcast schedules. Disabled when <= 0.",
    )
    return parser


async def run_async(args: argparse.Namespace) -> None:
    outdir = Path(args.outdir)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        area_map = await fetch_hls_map(session)

        if args.area not in area_map:
            raise SystemExit(
                "area='{}' was not found in config_web.xml. Available areas: {}".format(
                    args.area, ", ".join(sorted(area_map.keys()))
                )
            )

        manual_event_urls = list(args.event_url)
        series_ids = list(args.series_id)

        if not manual_event_urls and not series_ids:
            raise SystemExit("Specify either --event-url or --series-id.")

        scheduled_keys: set[str] = set()
        tasks: set[asyncio.Task[None]] = set()

        def _on_task_done(task: asyncio.Task[None]) -> None:
            tasks.discard(task)
            try:
                task.result()
            except Exception as exc:  # pragma: no cover - logging only
                print(f"[ERROR] Recording task raised an exception: {exc}")

        def _build_event_urls() -> List[str]:
            urls = list(manual_event_urls)
            if series_ids:
                dt_now = _datetime.datetime.now()
                query_to = (dt_now + _datetime.timedelta(days=1.0)).strftime("%Y-%m-%dT%H:%M")
                for series_id in series_ids:
                    base_url = f"https://api.nhk.jp/r7/f/broadcastevent/rs/{series_id}.json"
                    urls.append(f"{base_url}?to={query_to}&status=scheduled")
            return urls

        async def _schedule_new_events() -> int:
            added = 0
            for url in _build_event_urls():
                try:
                    events = await fetch_events(session, url)
                except Exception as exc:
                    print(f"[WARN] Failed to fetch broadcast schedule ({url}): {exc}")
                    continue

                for event in events:
                    event_key = f"{event.event_id or 'noid'}::{event.start.isoformat()}"
                    if not event.event_id:
                        event_key += f"::{event.title}"
                    if event_key in scheduled_keys:
                        continue

                    now = _datetime.datetime.now(tz=event.start.tzinfo)
                    if event.end <= now:
                        scheduled_keys.add(event_key)
                        continue

                    service = event.service or args.service or "r2"
                    area = (event.area or args.area).lower()

                    svc_map = area_map.get(area)
                    if not svc_map or service not in svc_map:
                        if service != "r2" and svc_map and "r2" in svc_map:
                            service = "r2"
                        elif svc_map:
                            available = ", ".join(sorted(svc_map.keys()))
                            raise SystemExit(
                                f"service='{service}' not found for area='{area}' (available: {available})."
                            )

                    hls = area_map.get(area, {}).get(service)
                    if not hls:
                        raise SystemExit(
                            f"Failed to resolve HLS URL: area={area}, service={service}"
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
                        scheduled_keys.add(event_key)
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
                    task.add_done_callback(_on_task_done)
                    tasks.add(task)
                    scheduled_keys.add(event_key)
                    added += 1
                    print(
                        f"[SCHEDULED] {event.title} ({service}@{area}) "
                        f"{event.start.isoformat()} → {event.end.isoformat()}"
                    )

            return added

        added_initial = await _schedule_new_events()

        if args.dry_run:
            return

        if added_initial == 0:
            print("No recording tasks were scheduled. Monitoring for new broadcasts.")

        refresh = max(0, args.refresh_sec)

        if refresh <= 0:
            # Wait for the current tasks without fetching new schedules
            if tasks:
                await asyncio.gather(*tasks)
            return

        while True:
            await asyncio.sleep(refresh)
            await _schedule_new_events()


def main(argv: List[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        asyncio.run(run_async(args))
    except KeyboardInterrupt:
        print("Interrupted.")
