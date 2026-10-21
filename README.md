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

## Editing the page
The browser code is minified. Edit the readable sources in `frontend/` (`app.js`, `config.js`,
`style.css`), then rebuild the files served from `app/static/`:

```bash
npm install
npm run build
```

Commit both `frontend/` and the rebuilt `app/static/app.js` / `style.css`.

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

## Deploy
This app needs a **long-running server** (downloads take seconds to minutes, ffmpeg merges files on
disk, and job progress is kept in memory). Serverless platforms like Vercel/Netlify Functions are a
poor fit: short time limits, ~4.5 MB response limit, and no shared disk between requests.

Use any Docker host instead — the included `Dockerfile` works as-is on:
- **Render** (Web Service → Docker) · **Railway** · **Fly.io** · **Koyeb** · or any VPS (`docker run -p 8000:8000`)

Custom domain: add a CNAME record for your subdomain (e.g. `dl` → the host's URL) and add the
domain in the host's dashboard; it issues HTTPS automatically.

Note: YouTube often blocks downloads from datacenter IPs ("Sign in to confirm you're not a bot").
Instagram/Facebook/TikTok are usually fine.
