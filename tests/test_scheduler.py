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


def test_run_scheduler_polls_for_additional_events(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    event1 = BroadcastEvent(
        broadcast_event_id="event-1",
        title="First Program",
        description=None,
        start=now + timedelta(minutes=5),
        end=now + timedelta(minutes=55),
        service_id="r1",
        area_id="130",
    )
    event2 = BroadcastEvent(
        broadcast_event_id="event-2",
        title="Second Program",
        description=None,
        start=now + timedelta(minutes=65),
        end=now + timedelta(minutes=125),
        service_id="r1",
        area_id="130",
    )

    catalog = StreamCatalog(
        area_slug="tokyo",
        area_name="Tokyo",
        area_key="130",
        station_id=None,
        streams={"r1": "https://example.invalid/stream.m3u8"},
    )

    call_count = 0

    async def fake_fetch_broadcast_events(session, series_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [event1]
        return [event1, event2]

    async def fake_fetch_stream_catalog(session):
        return {"130": catalog}

    scheduled_events: list[str] = []

    async def fake_execute_recording(plan, ffmpeg_path, log_level, dry_run):
        scheduled_events.append(plan.event.broadcast_event_id)
        await asyncio.sleep(0)

    monkeypatch.setattr(scheduler, "fetch_broadcast_events", fake_fetch_broadcast_events)
    monkeypatch.setattr(scheduler, "fetch_stream_catalog", fake_fetch_stream_catalog)
    monkeypatch.setattr(scheduler, "execute_recording", fake_execute_recording)

    async def orchestrate():
        task = asyncio.create_task(
            scheduler.run_scheduler(
                series_id="series",
                area="130",
                output_dir=tmp_path,
                lead_in_seconds=0,
                tail_out_seconds=0,
                default_duration_minutes=None,
                max_events=None,
                earliest_start=None,
                ffmpeg_path="ffmpeg",
                ffmpeg_log_level="error",
                dry_run=False,
                poll_interval_seconds=0.05,
            )
        )

        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(orchestrate())

    assert scheduled_events.count("event-1") == 1
    assert "event-2" in scheduled_events
    assert call_count >= 2
