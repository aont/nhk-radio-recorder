# NHK Radio Recorder (aiohttp + ffmpeg)

This project provides:

- **Backend recording service** with Python `aiohttp` and `ffmpeg`
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
python backend/app.py
```

Open http://localhost:8080 .

## API summary

- `GET /api/series`
- `GET /api/events?series_id=<id>&to_days=1`
- `GET /api/reservations`
- `POST /api/reservations`
- `DELETE /api/reservations/{reservation_id}`
- `GET /api/recordings`
- `PATCH /api/recordings/{recording_id}/metadata`
- `GET /api/recordings/{recording_id}/download`
- `POST /api/recordings/bulk-download`
- `DELETE /api/recordings/{recording_id}`

## Notes

- The scheduler loop runs every 30 seconds.
- Series list is cached for 6 hours.
- Event API 404 (HTTP or JSON payload) is treated as empty result.
- service_id mapping for streams follows:
  - `r1 -> r1`
  - `r2 -> r2`
  - `r3 -> fm`
