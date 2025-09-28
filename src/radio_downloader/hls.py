"""Utilities for working with NHK's HLS configuration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict

import aiohttp

CONFIG_XML_URL = "https://www.nhk.or.jp/radio/config/config_web.xml"


async def fetch_hls_map(session: aiohttp.ClientSession) -> Dict[str, Dict[str, str]]:
    """Return a mapping of ``area -> service -> HLS URL``."""

    headers = {"User-Agent": "nhk-radio-recorder/1.0 (+asyncio)"}
    async with session.get(CONFIG_XML_URL, headers=headers) as response:
        response.raise_for_status()
        text = await response.text()

    root = ET.fromstring(text)
    namespace = {}
    area_to_service: Dict[str, Dict[str, str]] = {}

    for data in root.findall(".//stream_url/data", namespace):
        area = (data.findtext("area") or "").strip()
        if not area:
            continue
        r1 = (data.findtext("r1hls") or "").strip()
        r2 = (data.findtext("r2hls") or "").strip()
        fm = (data.findtext("fmhls") or "").strip()
        services: Dict[str, str] = {}
        if r1:
            services["r1"] = r1
        if r2:
            services["r2"] = r2
        if fm:
            services["fm"] = fm
        if services:
            area_to_service[area] = services

    if not area_to_service:
        raise RuntimeError(
            "config_web.xml の解析に失敗しました。NHK側の仕様変更の可能性があります。"
        )

    return area_to_service


def pick_variant(url: str, variant: str) -> str:
    """Return ``url`` adjusted for the requested HLS variant."""

    if variant == "auto":
        return url
    if url.endswith("master.m3u8") and variant == "master48k":
        return url[: -len("master.m3u8")] + "master48k.m3u8"
    return url
