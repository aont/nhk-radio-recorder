"""Tools for assembling and running ffmpeg commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List


def build_ffmpeg_cmd(
    ffmpeg: str,
    hls_url: str,
    out_path: Path,
    duration_sec: int,
    copy_mode: bool = True,
    loglevel: str = "error",
) -> List[str]:
    """Return an ``ffmpeg`` command line suitable for recording an HLS stream."""

    base = [
        ffmpeg,
        "-nostats",
        "-loglevel",
        loglevel,
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_at_eof",
        "1",
        "-rw_timeout",
        "15000000",
        "-i",
        hls_url,
        "-vn",
        "-t",
        str(int(duration_sec)),
        "-y",
    ]

    if copy_mode:
        base += ["-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path)]
    else:
        base += ["-c:a", "libmp3lame", "-q:a", "2", str(out_path.with_suffix(".mp3"))]
    return base


async def run_ffmpeg(cmd: List[str]) -> int:
    """Execute ``ffmpeg`` asynchronously and return the exit status."""

    process = await asyncio.create_subprocess_exec(*cmd)
    return await process.wait()
