"""Social Media Video Downloader - FastAPI backend powered by yt-dlp."""

from __future__ import annotations

import asyncio
import shutil
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "smvd-downloads"
DOWNLOAD_ROOT.mkdir(exist_ok=True)

JOB_TTL_SECONDS = 30 * 60  # finished files are deleted after 30 minutes
MAX_WORKERS = 3
STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
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
    file_path: Path | None = None
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
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "socket_timeout": 30,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    return opts


def clean_error(exc: Exception) -> str:
    msg = str(exc)
    for prefix in ("ERROR: ", "[download] "):
        msg = msg.replace(prefix, "")
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
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none"):
            continue
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if f.get("acodec") in (None, "none"):
            size = size + best_audio if size else 0
        best_by_height[h] = max(best_by_height.get(h, 0), size)

    if not best_by_height and info.get("height"):
        best_by_height[info["height"]] = info.get("filesize") or 0

    options = []
    for std in STANDARD_HEIGHTS:
        # a bucket is offered if a format at least ~90% that tall exists (handles 1072p etc.)
        matching = [h for h in best_by_height if std * 0.9 <= h <= std * 1.1]
        if matching:
            h = max(matching)
            options.append({"height": std, "label": f"{std}p", "size": fmt_bytes(best_by_height[h])})
    return options


def platform_name(info: dict) -> str:
    return (info.get("extractor_key") or info.get("extractor") or "Web").replace(":", " ")


# --------------------------------------------------------------------------- #
# Download worker
# --------------------------------------------------------------------------- #
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
        "outtmpl": str(out_dir / "%(title).150B [%(id)s].%(ext)s"),
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
        if req.quality:
            h = req.quality
            opts["format"] = (
                f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
                f"bv*[height<={h}]+ba/"
                f"b[height<={h}]/bv*+ba/b"
            )
        else:
            opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
        if FFMPEG:
            opts["merge_output_format"] = "mp4"
        else:
            # without ffmpeg we can only take single-file (pre-merged) formats
            opts["format"] = f"b[height<={req.quality}]/b" if req.quality else "b"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([req.url])
        files = [p for p in out_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Download finished but no file was produced.")
        job.file_path = max(files, key=lambda p: p.stat().st_size)
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
        "title": info.get("title") or "Untitled",
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
        "filename": job.file_path.name if job.file_path else None,
    }


@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job.status != "done" or not job.file_path or not job.file_path.exists():
        raise HTTPException(404, "File not ready or expired.")
    return FileResponse(job.file_path, filename=job.file_path.name, media_type="application/octet-stream")


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
