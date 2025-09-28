# radio-downloader

This repository provides an asyncio-based scheduler that records NHK radio programmes by combining broadcast schedule JSON feeds with the HLS streams served by NHK. It orchestrates `ffmpeg` to capture shows automatically and can manage multiple reservations concurrently.

## Key features

- Automatically resolves per-area HLS master playlists from `config_web.xml`.
- Parses BroadcastEvent JSON documents flexibly to extract start/end timestamps and metadata.
- Falls back to MP3 re-encoding when a direct AAC copy fails.
- Supports `--dry-run` mode to inspect the reservation plan without invoking `ffmpeg`.
- Periodically refreshes schedules and schedules newly discovered programmes.

## Requirements

- Python 3.10+
- `ffmpeg` available on the system path

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use .venv\Scripts\activate
pip install -r requirements.txt
```

`sleep-absolute` enables high precision absolute-time sleeping where supported, but the tool also works without it.

Windows environments that lack the IANA time zone database may need:

```bash
pip install tzdata
```

The recorder still works in JST without `tzdata`, but installing it improves future compatibility.

## Usage

```bash
python main.py \
  --event-url "https://example.com/schedule.json" \
  --area tokyo \
  --service r2 \
  --outdir ./recordings
```

Notable options:

- `--event-url`: BroadcastEvent JSON URL(s). Provide multiple values separated by spaces.
- `--area`: `<area>` value from `config_web.xml` (for example `tokyo`, `osaka`).
- `--service`: One of `r1`, `r2`, `fm`. Use when the JSON does not specify the service.
- `--variant`: Select the HLS variant (`auto`, `master`, `master48k`).
- `--refresh-sec`: Interval in seconds to refresh schedules (default `300`). Use `0` or a negative value to disable refreshing.
- `--dry-run`: Show the planned recordings without running `ffmpeg`.

Run `python main.py --help` for the full CLI reference.

## Project layout

```
.
├── README.md
├── README.en.md
├── requirements.txt
├── main.py
├── docs/
│   ├── memo.md
│   └── memo_en.md
└── src/
    └── radio_downloader/
        ├── __init__.py
        ├── cli.py
        ├── events.py
        ├── ffmpeg.py
        ├── hls.py
        ├── models.py
        ├── recorder.py
        └── timing.py
```

## Further reading

Additional implementation notes are collected in [`docs/memo.md`](docs/memo.md) (Japanese) and [`docs/memo_en.md`](docs/memo_en.md) (English).
