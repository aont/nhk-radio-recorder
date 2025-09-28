"""Data models for NHK radio events."""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional


@dataclasses.dataclass
class NHKEvent:
    """Broadcast event metadata fetched from NHK's public APIs."""

    event_id: str
    title: str
    start: dt.datetime  # timezone-aware
    end: dt.datetime  # timezone-aware
    service: Optional[str] = None  # 'r1' / 'r2' / 'fm'
    area: Optional[str] = None  # 'tokyo', 'osaka', ...

    @property
    def duration(self) -> dt.timedelta:
        """Return the running time of the event."""

        return self.end - self.start
