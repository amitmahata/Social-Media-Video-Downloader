"""Social Media Video Downloader - FastAPI backend powered by yt-dlp."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "smvd-downloads"
DOWNLOAD_ROOT.mkdir(exist_ok=True)

JOB_TTL_SECONDS = 30 * 60  # finished files are deleted after 30 minutes
MAX_WORKERS = 3
STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]  # nothing below 360p is offered
AUDIO_BITRATES = [320, 192, 128]


def find_ffmpeg() -> str | None:
    """Prefer a system ffmpeg; fall back to the binary bundled with imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()

@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Social Media Video Downloader", lifespan=lifespan)
# compress HTML/CSS/JS/JSON only; downloads and images are already compressed
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    exclude_content_types=("text/event-stream", "application/octet-stream", "image/png", "video/mp4", "audio/mpeg"),
)
mimetypes.add_type("application/manifest+json", ".webmanifest")

# the page may be hosted elsewhere (e.g. Vercel) and call this API cross-origin
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "ALLOWED_ORIGINS", "https://save.amitmahata.com,http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
    max_age=86400,
)

LONG_CACHE = (".png", ".svg", ".ico", ".webmanifest")


@app.middleware("http")
async def seo_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or (request.url.hostname or "").startswith("api."):
        # the API host also serves a copy of the page — keep it (and the API) out of search
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    elif path.endswith(LONG_CACHE):
        response.headers["Cache-Control"] = "public, max-age=2592000"  # 30 days
    else:
        # HTML/CSS/JS, robots.txt, sitemap.xml: revalidate every time (cheap 304 via ETag)
        # so a new deploy shows up immediately
        response.headers["Cache-Control"] = "no-cache"
    return response
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    kind: Literal["video", "audio"] = "video"
    quality: int | None = None  # video height (e.g. 720) or audio bitrate (e.g. 192)


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued | downloading | processing | done | error
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    error: str = ""
    stage: str = ""  # human-readable step shown while processing
    file_path: Path | None = None
    download_name: str = ""
    created: float = field(default_factory=time.time)


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def validate_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Please paste a valid http(s) link.")
    return url


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "socket_timeout": 30,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    return opts


ANSI_RE = re.compile(chr(27) + r"\[[0-9;]*m")  # terminal colour codes
# Facebook/Instagram prefix titles with stats, e.g. "2.9K views · 3.9K reactions | "
STATS_PREFIX_RE = re.compile(r"^(?:[\d.,]+[KMB]?\s+(?:views|reactions|likes|comments|shares)\s*[·|]\s*)+", re.I)


def clean_title(info: dict) -> str:
    title = (info.get("title") or "").strip()
    # Instagram titles are just "Video by <user>"; the caption's first line is more useful
    if re.match(r"^(Video|Post) by ", title):
        caption = next((ln.strip() for ln in (info.get("description") or "").splitlines() if ln.strip()), "")
        if caption:
            title = caption[:120]
    title = STATS_PREFIX_RE.sub("", title).lstrip("| ").strip()
    uploader = info.get("uploader") or ""
    if uploader and title.endswith(f"| {uploader}"):
        title = title[: -len(uploader) - 2].rstrip(" |")
    return title or info.get("id") or "video"


def safe_filename(title: str, ext: str, max_len: int = 80) -> str:
    """Short, filesystem-safe file name for the user's download."""
    name = yt_dlp.utils.sanitize_filename(title, restricted=False)
    name = re.sub(r"\s+", " ", name).strip(" .") or "video"
    if len(name) > max_len:
        name = name[:max_len].rsplit(" ", 1)[0].rstrip(" .,-#") or name[:max_len]
    return f"{name}.{ext}"


def clean_error(exc: Exception) -> str:
    msg = ANSI_RE.sub("", str(exc))
    for prefix in ("ERROR: ", "[download] "):
        msg = msg.replace(prefix, "")
    if "not a bot" in msg or "Sign in to confirm" in msg:
        # YouTube blocks most cloud/datacenter servers regardless of the video
        return (
            "YouTube is blocking downloads from our server right now. "
            "Instagram, Facebook, TikTok, X and other sites still work — please try one of those."
        )
    if "Unsupported URL" in msg:
        return "This link isn't supported. Make sure it points to a public video or reel."
    if "login" in msg.lower() or "private" in msg.lower():
        return "This video is private or requires login, so it can't be downloaded."
    return msg.strip()[:400] or "Something went wrong."


def fmt_seconds(seconds: float | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_bytes(n: float | None) -> str | None:
    if not n:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def video_options(info: dict) -> list[dict]:
    """Map available formats onto standard quality buckets (1080p, 720p, ...)."""
    formats = info.get("formats") or []
    audio_sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    best_audio = max(audio_sizes, default=0)

    best_by_height: dict[int, int] = {}
    for f in formats:
        if f.get("vcodec") in (None, "none") or not f.get("height"):
            continue
        # quality is defined by the shorter side, so a 1080x1920 reel counts as 1080p
        h = min(f["height"], f.get("width") or f["height"])
        if not h:
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if f.get("acodec") in (None, "none"):
            size = size + best_audio if size else 0
        best_by_height[h] = max(best_by_height.get(h, 0), size)

    if not best_by_height and info.get("height"):
        h = min(info["height"], info.get("width") or info["height"])
        best_by_height[h] = info.get("filesize") or 0

    if not best_by_height:
        return []
    max_res = max(best_by_height)

    options = []
    for std in STANDARD_HEIGHTS:
        # a bucket is offered natively if a format ~that size exists (handles 1072p etc.)
        matching = [h for h in best_by_height if std * 0.9 <= h <= std * 1.1]
        if matching:
            h = max(matching)
            options.append({"height": std, "label": f"{std}p", "size": fmt_bytes(best_by_height[h])})
        elif std < max_res and FFMPEG:
            # not offered by the site: we downscale a higher version ourselves
            options.append({"height": std, "label": f"{std}p", "size": None, "converted": True})
    return options


def platform_name(info: dict) -> str:
    return (info.get("extractor_key") or info.get("extractor") or "Web").replace(":", " ")


# --------------------------------------------------------------------------- #
# Download worker
# --------------------------------------------------------------------------- #
VIDEO_SIZE_RE = re.compile(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b")


def probe_size(path: Path) -> tuple[int | None, int | None]:
    """Width/height of the first video stream, read from ffmpeg's stream summary."""
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    m = VIDEO_SIZE_RE.search(result.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def downscale(src: Path, quality: int, portrait: bool) -> Path:
    """Re-encode to H.264 with the shorter side set to `quality` (e.g. 480p)."""
    dst = src.with_name(f"{src.stem}.{quality}p.mp4")
    # -2 keeps the aspect ratio with an even dimension (required by H.264)
    scale = f"scale={quality}:-2" if portrait else f"scale=-2:{quality}"
    cmd = [
        FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
        "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError(f"Couldn't convert to {quality}p: {result.stderr.strip()[-300:]}")
    src.unlink(missing_ok=True)
    return dst


def run_download(job: Job, req: DownloadRequest) -> None:
    out_dir = DOWNLOAD_ROOT / job.id
    out_dir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                job.progress = round(d.get("downloaded_bytes", 0) / total * 100, 1)
            job.status = "downloading"
            job.speed = (fmt_bytes(d.get("speed")) or "") + ("/s" if d.get("speed") else "")
            job.eta = fmt_seconds(d.get("eta"))
        elif d["status"] == "finished":
            job.status = "processing"
            job.progress = 100.0

    opts = base_opts() | {
        # save under the short video id (long captions overflow Windows' 260-char path limit);
        # the friendly name is applied when the file is served
        "outtmpl": str(out_dir / "%(id).60B.%(ext)s"),
        "progress_hooks": [hook],
        "postprocessor_hooks": [lambda d: setattr(job, "status", "processing")],
    }

    if req.kind == "audio":
        bitrate = req.quality if req.quality in AUDIO_BITRATES else 192
        opts["format"] = "bestaudio/best"
        if FFMPEG:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)}
            ]
    else:
        # "res" sorts by the shorter side, so portrait reels and landscape videos both work;
        # "res:720" means "best resolution not above 720p"
        sort = ["res", "ext:mp4:m4a"]
        if req.quality:
            sort[0] = f"res:{req.quality}"
        opts["format_sort"] = sort
        if FFMPEG:
            opts["format"] = "bv*+ba/b"
            opts["merge_output_format"] = "mp4"
        else:
            # without ffmpeg we can only take single-file (pre-merged) formats
            opts["format"] = "b"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = next((e for e in info["entries"] if e), info)
        files = [p for p in out_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Download finished but no file was produced.")
        job.file_path = max(files, key=lambda p: p.stat().st_size)

        # the site had nothing at/under the requested quality: shrink it ourselves.
        # measure the real file — sites don't always report a resolution for every format
        w, h = probe_size(job.file_path) if req.kind == "video" and req.quality and FFMPEG else (None, None)
        if w and h and min(w, h) > req.quality * 1.1:
            job.status = "processing"
            job.stage = f"Converting to {req.quality}p…"
            job.file_path = downscale(job.file_path, req.quality, portrait=h > w)

        job.download_name = safe_filename(clean_title(info), job.file_path.suffix.lstrip("."))
        job.status = "done"
        job.progress = 100.0
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = clean_error(exc)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.post("/api/info")
async def get_info(body: InfoRequest):
    url = validate_url(body.url)

    def extract():
        with yt_dlp.YoutubeDL(base_opts() | {"skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.get_running_loop().run_in_executor(executor, extract)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, clean_error(exc)) from exc

    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    return {
        "title": clean_title(info),
        "thumbnail": info.get("thumbnail"),
        "duration": fmt_seconds(info.get("duration")),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "platform": platform_name(info),
        "video_options": video_options(info),
        "audio_options": [{"bitrate": b, "label": f"MP3 · {b} kbps"} for b in AUDIO_BITRATES]
        if FFMPEG
        else [{"bitrate": 0, "label": "Original audio"}],
    }


@app.post("/api/download")
async def start_download(body: DownloadRequest):
    body.url = validate_url(body.url)
    job = Job(id=uuid.uuid4().hex)
    with jobs_lock:
        jobs[job.id] = job
    executor.submit(run_download, job, body)
    return {"job_id": job.id}


@app.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired.")
    return {
        "status": job.status,
        "progress": job.progress,
        "speed": job.speed,
        "eta": job.eta,
        "error": job.error,
        "stage": job.stage,
        "filename": job.download_name or None,
    }


@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job.status != "done" or not job.file_path or not job.file_path.exists():
        raise HTTPException(404, "File not ready or expired.")
    return FileResponse(
        job.file_path, filename=job.download_name or job.file_path.name, media_type="application/octet-stream"
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "ffmpeg": bool(FFMPEG), "yt_dlp": yt_dlp.version.__version__}


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
async def cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        with jobs_lock:
            expired = [j for j in jobs.values() if now - j.created > JOB_TTL_SECONDS]
            for j in expired:
                jobs.pop(j.id, None)
        for j in expired:
            shutil.rmtree(DOWNLOAD_ROOT / j.id, ignore_errors=True)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
