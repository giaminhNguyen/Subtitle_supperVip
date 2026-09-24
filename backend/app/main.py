from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .database import SessionLocal, get_db
from .models import Channel, ChannelSettings, Job, JobLog, JobStatus, Subtitle, Video, VideoStatus
from .schemas import ChannelCreate, ChannelOut, ChannelSettingsUpdate, ScanRequest, VideoOut, YouTubeApiKeyUpdate
from .services.health import diagnostics, health_summary
from .services.jobs import enqueue
from .services.runtime_settings import set_youtube_api_key, youtube_api_key_configured
from .services.subtitles import BlockedByYouTube, SubtitleUnavailable, available_transcripts
from .services.syncruns import refresh_sync_run_state
from .services.youtube import YouTubeDataClient, YouTubeError

app = FastAPI(title="YouTube Subtitle Manager", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins.split(","), allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Total-Count"]
)


def paged(db: Session, response: Response, query, order_by: tuple, limit: int, offset: int):
    """Stable page of `query` (list body stays a plain array; the total goes in X-Total-Count)."""
    response.headers["X-Total-Count"] = str(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    return db.scalars(
        query.order_by(
            *order_by,
        )
        .limit(limit)
        .offset(offset)
    ).all()


def channel_or_404(db: Session, channel_id: str) -> Channel:
    item = db.get(Channel, channel_id)
    if not item:
        raise HTTPException(404, "Không tìm thấy kênh")
    return item


def video_or_404(db: Session, video_id: str) -> Video:
    item = db.get(Video, video_id)
    if not item:
        raise HTTPException(404, "Không tìm thấy video")
    return item


@app.get("/health")
def health():
    """Lightweight liveness/readiness: 200 unless the database or storage is failing (503)."""
    db = SessionLocal()
    try:
        body, code = health_summary(db)
    except Exception:
        body, code = {"ok": False, "status": "critical", "api": "ok", "database": "error"}, 503
    finally:
        db.close()
    return JSONResponse(body, status_code=code)


@app.get("/api/diagnostics")
def get_diagnostics(db: Session = Depends(get_db)):
    return diagnostics(db)


@app.get("/api/config/youtube")
def youtube_config():
    return {"configured": youtube_api_key_configured()}


@app.put("/api/config/youtube")
def update_youtube_config(body: YouTubeApiKeyUpdate):
    try:
        set_youtube_api_key(body.api_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"configured": True}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)):
    count = lambda query: db.scalar(query) or 0
    return {
        "channels": count(select(func.count(Channel.id))),
        "videos": count(select(func.count(Video.id))),
        "completed": count(select(func.count(Video.id)).where(Video.status == VideoStatus.completed)),
        "failed": count(select(func.count(Video.id)).where(Video.status.in_([VideoStatus.failed, VideoStatus.blocked]))),
        "no_subtitle": count(select(func.count(Video.id)).where(Video.status == VideoStatus.no_subtitle)),
        "running_jobs": count(select(func.count(Job.id)).where(Job.status == JobStatus.processing)),
    }


@app.post("/api/channels", response_model=ChannelOut, status_code=201)
def create_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    try:
        resolved = YouTubeDataClient().resolve_channel(body.url)
    except (ValueError, YouTubeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    existing = db.scalar(select(Channel).where(Channel.youtube_channel_id == resolved.channel_id))
    if existing:
        return existing
    channel = Channel(
        youtube_channel_id=resolved.channel_id, url=body.url.strip(), title=resolved.title, avatar_url=resolved.avatar_url, uploads_playlist_id=resolved.uploads_playlist_id
    )
    channel.settings = ChannelSettings()
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


@app.get("/api/channels", response_model=list[ChannelOut])
def list_channels(db: Session = Depends(get_db)):
    return db.scalars(select(Channel).order_by(Channel.added_at.desc())).all()


@app.get("/api/channels/{channel_id}")
def get_channel(channel_id: str, db: Session = Depends(get_db)):
    channel = channel_or_404(db, channel_id)
    return {
        "id": channel.id,
        "youtube_channel_id": channel.youtube_channel_id,
        "url": channel.url,
        "title": channel.title,
        "avatar_url": channel.avatar_url,
        "added_at": channel.added_at,
        "video_count": channel.video_count,
        "last_scanned_at": channel.last_scanned_at,
        "next_sync_at": channel.next_sync_at,
        "settings": channel.settings,
    }


@app.put("/api/channels/{channel_id}/settings")
def update_channel_settings(channel_id: str, body: ChannelSettingsUpdate, db: Session = Depends(get_db)):
    channel = channel_or_404(db, channel_id)
    for key, value in body.model_dump().items():
        setattr(channel.settings, key, value)
    db.commit()
    return channel.settings


@app.post("/api/channels/{channel_id}/scan", status_code=202)
def queue_scan(channel_id: str, body: ScanRequest, db: Session = Depends(get_db)):
    channel_or_404(db, channel_id)
    if body.mode == "since" and not body.since:
        raise HTTPException(422, "Chế độ since cần ngày bắt đầu")
    payload = {"mode": body.mode, "since": body.since.isoformat() if body.since else None}
    job = enqueue(db, "scan", channel_id=channel_id, payload=payload)
    db.commit()
    return {"job_id": job.id, "status": job.status}


@app.post("/api/channels/{channel_id}/sync", status_code=202)
def sync_new(channel_id: str, db: Session = Depends(get_db)):
    channel_or_404(db, channel_id)
    job = enqueue(db, "scan", channel_id=channel_id, payload={"mode": "new"})
    db.commit()
    return {"job_id": job.id, "status": job.status}


@app.get("/api/channels/{channel_id}/videos", response_model=list[VideoOut])
def list_channel_videos(
    channel_id: str,
    response: Response,
    status: VideoStatus | None = None,
    q: str | None = None,
    language: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    channel_or_404(db, channel_id)
    query = select(Video).options(selectinload(Video.subtitles)).where(Video.channel_id == channel_id)
    if status:
        query = query.where(Video.status == status)
    if q:
        query = query.where(Video.title.ilike(f"%{q}%"))
    if language:
        query = query.where(Video.id.in_(select(Subtitle.video_id).where(Subtitle.language_code == language)))  # subquery, not a join: keeps rows and totals unique
    return paged(db, response, query, (Video.published_at.desc(), Video.id), limit, offset)


@app.get("/api/videos/{video_id}", response_model=VideoOut)
def get_video(video_id: str, db: Session = Depends(get_db)):
    return db.scalar(select(Video).options(selectinload(Video.subtitles)).where(Video.id == video_id)) or (_ for _ in ()).throw(HTTPException(404, "Không tìm thấy video"))


@app.get("/api/videos/{video_id}/available-subtitles")
def available_subtitles(video_id: str, db: Session = Depends(get_db)):
    video = video_or_404(db, video_id)
    try:
        return available_transcripts(video.youtube_video_id)
    except SubtitleUnavailable:
        return []
    except BlockedByYouTube as exc:
        raise HTTPException(429, f"YouTube có thể đã chặn IP: {exc}") from exc
    except Exception as exc:
        raise HTTPException(502, f"Không thể kiểm tra subtitle: {exc}") from exc


@app.post("/api/videos/{video_id}/download", status_code=202)
def download_video(video_id: str, force: bool = False, db: Session = Depends(get_db)):
    video = video_or_404(db, video_id)
    if video.status == VideoStatus.completed and not force:
        raise HTTPException(409, "Video đã hoàn tất; dùng force=true để tải lại")
    if force:
        video.status = VideoStatus.pending
    job = enqueue(db, "download", channel_id=video.channel_id, video_id=video.id, payload={"force": force})
    db.commit()
    return {"job_id": job.id, "status": job.status}


@app.get("/api/videos")
def all_videos(response: Response, status: VideoStatus | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(Video).options(selectinload(Video.subtitles))
    if status:
        query = query.where(Video.status == status)
    return paged(db, response, query, (Video.published_at.desc(), Video.id), limit, offset)


@app.get("/api/jobs")
def jobs(response: Response, status: JobStatus | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(Job)
    if status:
        query = query.where(Job.status == status)
    return paged(db, response, query, (Job.created_at.desc(), Job.id), limit, offset)


@app.post("/api/jobs/{job_id}/{action}")
def control_job(job_id: str, action: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy job")
    if action == "pause" and job.status == JobStatus.queued:
        job.status = JobStatus.paused
    elif action == "resume" and job.status == JobStatus.paused:
        job.status = JobStatus.queued
    elif action == "cancel" and job.status in [JobStatus.queued, JobStatus.paused]:
        job.status = JobStatus.cancelled
    elif action == "retry" and job.status == JobStatus.failed:
        clash = db.scalar(
            select(Job.id)
            .where(
                Job.id != job.id,
                Job.kind == job.kind,
                Job.channel_id == job.channel_id,
                Job.video_id == job.video_id,
                Job.status.in_([JobStatus.queued, JobStatus.processing, JobStatus.paused]),
            )
            .limit(1)
        )
        if clash:
            raise HTTPException(409, "Đã có job đang hoạt động cho mục này; không cần thử lại job cũ")
        job.status, job.attempts, job.scheduled_at, job.error, job.worker_id, job.lease_expires_at, job.outcome = JobStatus.queued, 0, datetime.utcnow(), None, None, None, None
    else:
        raise HTTPException(409, "Không thể thực hiện thao tác với trạng thái hiện tại")
    try:
        db.flush()
        refresh_sync_run_state(db, job.sync_run_id)  # pause/cancel/retry change what the run is waiting for
        db.commit()
    except IntegrityError:  # lost a race with another active job for the same target (partial unique index)
        db.rollback()
        raise HTTPException(409, "Đã có job đang hoạt động cho mục này") from None
    return job


@app.get("/api/logs")
def logs(response: Response, job_id: str | None = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = select(JobLog)
    if job_id:
        query = query.where(JobLog.job_id == job_id)
    return paged(db, response, query, (JobLog.created_at.desc(), JobLog.id), limit, offset)
