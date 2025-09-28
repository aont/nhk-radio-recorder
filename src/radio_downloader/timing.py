"""Helpers for sleeping until an absolute timestamp."""

from __future__ import annotations

import asyncio
import datetime as dt

try:  # pragma: no cover - depends on optional dependency
    from sleep_absolute import wait_until as abs_wait_until  # type: ignore
except Exception:  # pragma: no cover - fallback when library is missing
    abs_wait_until = None  # type: ignore


async def sleep_until(target: dt.datetime) -> None:
    """Sleep until ``target`` while keeping the asyncio loop responsive."""

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
            pass

    delta = (target - dt.datetime.now(tz=target.tzinfo or dt.timezone.utc)).total_seconds()
    await asyncio.sleep(max(0.0, delta))
