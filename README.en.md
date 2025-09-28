# radio-downloader

An asynchronous tool for saving NHK radio programs from HLS streams with ffmpeg. It retrieves broadcast schedules from NHK's public API and queues ffmpeg executions so that recording starts at the right time for the specified series.

## Requirements

- Python 3.11 or later
- ffmpeg

Install the Python dependencies with `pip install -r requirements.txt`.

## Usage

```
python -m radio_downloader <series_id> [options]
```

### Main options

| Option | Description | Default |
| --- | --- | --- |
| `--area` | Area key or slug, e.g. `130` (Tokyo), `osaka` | `130` |
| `--output-dir` | Directory where recordings are stored | `recordings` |
| `--lead-in` | Seconds to start recording before the broadcast starts | `60` |
| `--tail-out` | Seconds to keep recording after the broadcast ends | `120` |
| `--default-duration` | Fallback length in minutes when the end time is unavailable | None |
| `--max-events` | Number of upcoming broadcasts to schedule | `1` |
| `--start-after` | Ignore broadcasts that start before the provided ISO timestamp | None |
| `--dry-run` | Show the planned recordings without running ffmpeg | - |
| `--verbose` | Show verbose logs | - |
| `--poll-interval` | Interval in seconds for re-fetching the schedule | `900` |

### Example

The following command records the next broadcast of "Best of Classic" (series ID `Z9L1V2M24L`) in the Tokyo FM area, starting 60 seconds early and continuing for 3 minutes after the scheduled end time:

```
python -m radio_downloader Z9L1V2M24L --area tokyo --lead-in 60 --tail-out 180
```

Recordings are saved in the `recordings` directory with filenames in the format `YYYYMMDDTHHMMSSZ_<title>.m4a`. When the `--dry-run` option is used the tool only prints the planned recordings instead of launching ffmpeg. By default the scheduler refreshes the broadcast schedule every 15 minutes and automatically adds newly found programs to the queue.
