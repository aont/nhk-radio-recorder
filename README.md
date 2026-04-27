# NHK Radio Recorder (aiohttp + ffmpeg)

This project provides:

- **Backend recording service** with Python `aiohttp` and `ffmpeg`
- Optional **YouTube Music upload** via `ytmusicapi` after each recording completes
- **Frontend (HTML/CSS/JavaScript)** for:
  - adding/removing reservations
  - playback of HLS recordings
  - single recording download (m4a conversion on demand)
  - recording deletion
  - metadata editing
  - bulk download for multiple recordings as **ZIP (stored/no compression)**
- Two reservation styles:
  - reserve a **single broadcast event**
  - register a **series watcher** that periodically checks upcoming events and auto-creates single-event reservations

## Tech constraints

- No Node.js/npm build tools are used.
- HLS is stored on disk for recordings.
- m4a is generated only when download is requested.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python backend/app.py --port 8080
```

Open http://localhost:8080 .

Use `--debug-log` if you want verbose backend logs.
NHK API JSON fetch debug logs (`[debug] GET JSON ...`) are disabled by default. Enable them with `--debug-nhk-json-log` (or `NHK_DEBUG_JSON_LOG=1`).

## API summary

- `GET /series`
- `GET /series/resolve?series_url=<url>`
- `GET /events?series_code=<code>&series_url=<url>&series_id=<id>`
- `GET /reservations`
- `POST /reservation/single-event`
- `POST /reservation/watch-series`
- `DELETE /reservations/{reservation_id}`
- `GET /recordings`
- `PATCH /recordings/{recording_id}/metadata`
- `GET /recordings/{recording_id}/download`
- `POST /recordings/bulk-download`
- `DELETE /recordings/{recording_id}`


## Debug logging

- Backend: launch with `--debug-log` to emit detailed logs from NHK fetch paths, `/events`, and YouTube auto upload flow.
- Backend (NHK API JSON only): launch with `--debug-nhk-json-log` (or set `NHK_DEBUG_JSON_LOG=1`) to emit `GET JSON` request/response logs.
- Frontend: open the app with `?debug=1` (for example `http://localhost:8080/?debug=1`) or set `localStorage["nhkRadioRecorder.debugLog"] = "1"` in DevTools.

## Notes

- The scheduler loop runs every 30 seconds.
- Series list is cached for 6 hours.
- Event API 404 (HTTP or JSON payload) is treated as empty result.
- service_id mapping for streams follows:
  - `r1 -> r1`
  - `r2 -> r2`
  - `r3 -> fm`


## YouTube Music upload

- Add one or more YouTube accounts from the **Configuration** tab.
- Each account needs an alias and HTTP headers JSON required by `ytmusicapi`.
- Every completed recording is uploaded to all configured accounts.
