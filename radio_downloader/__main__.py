"""Command line interface for the radio downloader."""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .scheduler import run_scheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule NHK radio recordings via ffmpeg.")
    parser.add_argument("series_id", help="NHK radio series identifier (e.g. Z9L1V2M24L)")
    parser.add_argument(
        "--area",
        default="130",
        help="Area key or slug (default: 130 / Tokyo)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("recordings"),
        help="Directory where recordings will be saved",
    )
    parser.add_argument(
        "--lead-in",
        type=int,
        default=60,
        help="Seconds to start recording before the scheduled start",
    )
    parser.add_argument(
        "--tail-out",
        type=int,
        default=120,
        help="Seconds to continue recording after the scheduled end",
    )
    parser.add_argument(
        "--default-duration",
        type=int,
        default=None,
        help="Default duration in minutes when end time is missing",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=1,
        help="Maximum number of upcoming events to schedule (default: 1)",
    )
    parser.add_argument(
        "--start-after",
        type=str,
        default=None,
        help="Ignore events starting before the provided ISO timestamp",
    )
    parser.add_argument(
        "--ffmpeg",
        type=str,
        default="ffmpeg",
        help="Path to the ffmpeg executable",
    )
    parser.add_argument(
        "--ffmpeg-log-level",
        type=str,
        default="error",
        help="Log level passed to ffmpeg",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned recordings without running ffmpeg",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Increase logging verbosity",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=900,
        help="Seconds between schedule refresh checks (default: 900)",
    )
    return parser


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    earliest_start = parse_datetime(args.start_after)

    configure_logging(args.verbose)

    try:
        asyncio.run(
            run_scheduler(
                series_id=args.series_id,
                area=args.area,
                output_dir=args.output_dir,
                lead_in_seconds=args.lead_in,
                tail_out_seconds=args.tail_out,
                default_duration_minutes=args.default_duration,
                max_events=args.max_events,
                earliest_start=earliest_start,
                ffmpeg_path=args.ffmpeg,
                ffmpeg_log_level=args.ffmpeg_log_level,
                dry_run=args.dry_run,
                poll_interval_seconds=max(args.poll_interval, 0),
            )
        )
    except KeyboardInterrupt:
        parser.exit(1, "Interrupted by user\n")
    except Exception as exc:  # noqa: BLE001
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
