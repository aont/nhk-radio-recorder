import asyncio

import aiohttp

from radio_downloader import nhk


def test_fetch_broadcast_events_handles_error_payload(monkeypatch):
    async def fake_json_request(session, url):
        return {"error": {"statuscode": 404, "message": "Not Found."}}

    monkeypatch.setattr(nhk, "_json_request", fake_json_request)

    events = asyncio.run(nhk.fetch_broadcast_events(object(), "series"))

    assert events == []


def test_fetch_broadcast_events_handles_404_exception(monkeypatch):
    async def fake_json_request(session, url):
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=(),
            status=404,
            message="Not Found",
            headers=None,
        )

    monkeypatch.setattr(nhk, "_json_request", fake_json_request)

    events = asyncio.run(nhk.fetch_broadcast_events(object(), "series"))

    assert events == []
