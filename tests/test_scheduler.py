import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from radio_downloader.nhk import BroadcastEvent, StreamCatalog
from radio_downloader import scheduler


def test_prepare_plans_accepts_area_slug(monkeypatch, tmp_path):
    event = BroadcastEvent(
        broadcast_event_id="event-1",
        title="Sample Program",
        description=None,
        start=datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc),
        end=datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
        service_id="r1",
        area_id="tokyo",
    )

    catalog = StreamCatalog(
        area_slug="tokyo",
        area_name="Tokyo",
        area_key="130",
        station_id=None,
        streams={"r1": "https://example.invalid/stream.m3u8"},
    )

    async def fake_fetch_broadcast_events(session, series_id):
        return [event]

    async def fake_fetch_stream_catalog(session):
        return {"130": catalog, "tokyo": catalog}

    monkeypatch.setattr(scheduler, "fetch_broadcast_events", fake_fetch_broadcast_events)
    monkeypatch.setattr(scheduler, "fetch_stream_catalog", fake_fetch_stream_catalog)

    session = object()

    plans = asyncio.run(
        scheduler.prepare_plans(
            session=session,
            series_id="series",
            area_key="tokyo",
            output_dir=tmp_path,
            lead_in=timedelta(0),
            tail_out=timedelta(0),
            default_duration=None,
        )
    )

    assert len(plans) == 1
    assert plans[0].event == event
    assert plans[0].stream_catalog == catalog
