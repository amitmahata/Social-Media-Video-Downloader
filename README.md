# Social Media Video Downloader

Paste a link from YouTube, Instagram (reels/posts), Facebook, TikTok, X/Twitter, Reddit, Vimeo
and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), then
download the video in a standard quality (144p – 4K, whatever the source offers) as MP4,
or extract the audio only as MP3 (128 / 192 / 320 kbps).

## Stack
- **Backend:** Python + FastAPI, [yt-dlp](https://github.com/yt-dlp/yt-dlp) for extraction,
  ffmpeg (bundled via `imageio-ffmpeg`, or your system ffmpeg if installed) for merging and MP3 conversion.
- **Frontend:** plain HTML/CSS/JS (no build step), served by FastAPI.

## Run (Windows)
Double-click `run.bat`, or:

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Then open http://localhost:8000.

## API
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/info` `{url}` | Title, thumbnail, available qualities |
| POST | `/api/download` `{url, kind: "video"\|"audio", quality}` | Starts a job, returns `job_id` |
| GET | `/api/progress/{job_id}` | Status / percent / speed / ETA |
| GET | `/api/file/{job_id}` | Downloads the finished file |
| GET | `/api/health` | ffmpeg + yt-dlp version check |

Finished files are kept in your temp folder for 30 minutes, then deleted.

## Notes
- Sites change often — if a platform stops working, update yt-dlp:
  `.venv\Scripts\python -m pip install -U yt-dlp`
- Private videos or ones that require login can't be downloaded.
- Only download content you own or have permission to use, and respect each platform's terms.
