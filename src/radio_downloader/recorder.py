"""Core recording workflow for NHK radio streams."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from .ffmpeg import build_ffmpeg_cmd, run_ffmpeg
from .models import NHKEvent
from .timing import sleep_until
from .utils import JP_TZ, sanitize_filename


async def record_one(
    event: NHKEvent,
    hls_url: str,
    outdir: Path,
    prepad: int,
    postpad: int,
    ffmpeg_path: str,
    loglevel: str = "error",
) -> None:
    """Record a single :class:`NHKEvent` instance."""

    start_at = event.start - dt.timedelta(seconds=max(0, prepad))
    stop_at = event.end + dt.timedelta(seconds=max(0, postpad))
    now = dt.datetime.now(tz=event.start.tzinfo)
    if stop_at <= now:
        print(f"[SKIP] {event.title} has already finished: stop {stop_at.isoformat()}")
        return

    stamp = event.start.astimezone(JP_TZ).strftime("%Y%m%d_%H%M")
    base = f"{stamp}_{sanitize_filename(event.title)}"
    out = outdir / f"{base}.m4a"

    if start_at > now:
        print(f"[WAIT] {event.title} will start at {start_at.isoformat()} (HLS: {hls_url})")
        await sleep_until(start_at)
    else:
        print(
            "[LATE START] The scheduled start time has already passed. Starting now. "
            f"{now.isoformat()} > {start_at.isoformat()}"
        )

    now2 = dt.datetime.now(tz=event.start.tzinfo)
    duration = int((stop_at - now2).total_seconds())
    if duration <= 0:
        print(f"[SKIP] Recording duration is zero or negative: {event.title}")
        return

    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_ffmpeg_cmd(
        ffmpeg_path,
        hls_url,
        out,
        duration_sec=duration,
        copy_mode=True,
        loglevel=loglevel,
    )
    print(f"[FFMPEG] {' '.join(cmd)}")
    rc = await run_ffmpeg(cmd)
    if rc == 0:
        print(f"[DONE] {out}")
        return

    print(
        f"[RETRY] Copy-to-M4A failed; retrying with libmp3lame re-encode (rc={rc})"
    )
    cmd2 = build_ffmpeg_cmd(
        ffmpeg_path,
        hls_url,
        out,
        duration_sec=duration,
        copy_mode=False,
        loglevel=loglevel,
    )
    print(f"[FFMPEG] {' '.join(cmd2)}")
    rc2 = await run_ffmpeg(cmd2)
    if rc2 == 0:
        print(f"[DONE] {out.with_suffix('.mp3')}")
    else:
        print(f"[FAIL] ffmpeg failed rc={rc2}")
