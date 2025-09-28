# Notes for the NHK radio downloader (asyncio + ffmpeg)

This memo documents the asyncio-based tool that reserves NHK radio recordings by combining BroadcastEvent JSON schedules with `ffmpeg`. It fetches programme information from the broadcast schedule API and resolves the HLS playlists listed in `config_web.xml`.

---

## Specification overview

* Inputs:
  * **Broadcast schedule JSON URL** (for example `https://api.nhk.jp/r7/f/broadcastevent/rs/Z9L1V2M24L.json`). The script extracts `start` / `end` timestamps and metadata from the payload.
  * **Area** and **service** (`r1` / `r2` / `fm`) can be overridden on the CLI when they cannot be inferred from the JSON (defaults: `tokyo` & `r2`).
* HLS URL resolution:
  * Fetch `https://www.nhk.or.jp/radio/config/config_web.xml` and parse the `r1hls` / `r2hls` / `fmhls` entries per area. Recently NHK has been distributing master playlists such as `radio-stream.nhk.jp/.../master.m3u8`; switching to `master48k.m3u8` is possible if required. ([Zenn][1])
* Scheduling:
  * Use `wait_until()` from `python-sleep-absolute` for precise absolute sleeps (non-blocking on Linux/Windows; falls back to `asyncio.sleep` elsewhere). ([GitHub][2])
* Recording:
  * Invoke `ffmpeg` with `-c copy` to store `.m4a` without re-encoding. Add `-bsf:a aac_adtstoasc` if necessary. See the gihyo.jp article for additional HLS recording tips. ([gihyo.jp][3])
* When the JSON contains multiple events the script schedules all of them concurrently with asyncio tasks.
* Apply `-reconnect` options to mitigate short network interruptions.
* `--prepad` / `--postpad` add margins before and after the broadcast window.
* `--dry-run` prints the planned recordings without launching `ffmpeg`.

> **Notice**: Recorded audio remains the property of NHK. Use the tool only within the scope of private copying. ([Zenn][1])

---

## Usage notes

```bash
# Install dependencies (Python 3.11+ recommended)
pip install -r requirements.txt

# Example: record the Tokyo R2 stream using a broadcast schedule JSON URL
python main.py \
  --event-url "https://api.nhk.jp/r7/f/broadcastevent/rs/Z9L1V2M24L.json" \
  --area tokyo --service r2 \
  --outdir ./recordings --postpad 30 --prepad 5

# Preview only (no recording)
python main.py \
  --event-url "..." --area tokyo --service r2 --dry-run
```

> Area/service options override the JSON when they cannot be resolved automatically.
> Actual HLS URLs are taken from `config_web.xml` (for example `.../nhkradiruakr2/master.m3u8`). ([Zenn][1])

---

## Implementation notes

The codebase lives under `src/radio_downloader/`, split into modules that `main.py` wires together:

- `cli.py`: CLI parsing and orchestration
- `events.py`: Broadcast schedule parsing
- `hls.py`: Fetch `config_web.xml` and resolve HLS URLs
- `recorder.py`: Recording task implementation
- `ffmpeg.py`: Build and execute `ffmpeg` commands
- `timing.py`: Abstractions for absolute-time sleeping

The migration from the original single-file script moved core functions into dedicated modules while preserving behaviour.

[1]: https://zenn.dev/articles/nhk-radio-hls
[2]: https://github.com/aont/python-sleep-absolute
[3]: https://gihyo.jp/article/2020/ffmpeg-hls
