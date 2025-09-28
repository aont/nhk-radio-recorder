"""General utility helpers used across the project."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    JP_TZ = ZoneInfo("Asia/Tokyo")
except ZoneInfoNotFoundError:
    # ``tzdata`` is not bundled with the Windows installer, so fall back to a
    # fixed-offset timezone when the IANA database is unavailable. The
    # difference between UTC+09:00 and ``Asia/Tokyo`` is negligible for NHK
    # programming schedules which do not observe DST.
    JP_TZ = dt.timezone(dt.timedelta(hours=9))


def parse_iso8601(value: Any, default_tz: ZoneInfo = JP_TZ) -> Optional[dt.datetime]:
    """Convert loosely formatted ISO-8601 strings or Unix seconds into ``datetime``."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone(default_tz)

    s = str(value).strip()
    if not s:
        return None

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            d = dt.datetime.strptime(s, "%Y%m%d%H%M%S")
            d = d.replace(tzinfo=default_tz)
        except Exception:
            return None

    if d.tzinfo is None:
        d = d.replace(tzinfo=default_tz)
    return d


def any_key(mapping: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    """Return the first value associated with the provided keys in ``mapping``."""

    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def sanitize_filename(name: str) -> str:
    """Clean up strings so they are safe for use as filenames."""

    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1F]', "_", name)
    name = re.sub(r"_+", "_", name)
    return name[:120] or "untitled"
