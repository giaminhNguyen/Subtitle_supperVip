from datetime import datetime, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Channel, Job, JobLog, JobStatus, SyncRun, Video, VideoStatus
from .youtube import YouTubeDataClient


def log(db: Session, job: Job | None, message: str, level: str = "info"):
    db.add(JobLog(job_id=job.id if job else None, message=message, level=level))


def enqueue(db: Session, kind: str, *, channel_id: str | None = None, video_id: str | None = None, payload: dict | None = None) -> Job:
    # Prevent duplicate active work for the same target/type.
    existing = db.scalar(select(Job).where(Job.kind == kind, Job.channel_id == channel_id, Job.video_id == video_id, Job.status.in_([JobStatus.queued, JobStatus.processing, JobStatus.paused])))
    if existing: return existing
    job = Job(kind=kind, channel_id=channel_id, video_id=video_id, payload=payload or {}, max_attempts=settings.max_job_attempts)
    db.add(job)
    if video_id:
        video = db.get(Video, video_id)
        if video and video.status != VideoStatus.completed: video.status = VideoStatus.queued
    return job


def scan_channel(db: Session, channel: Channel, mode: str, since: datetime | None = None, checkpoint=None) -> tuple[int, int]:
    """`checkpoint()` runs before every commit; the worker uses it to prove it still owns the job."""
    checkpoint = checkpoint or (lambda: None)
    if isinstance(since, str):
        from dateutil.parser import isoparse
        since = isoparse(since).replace(tzinfo=None)
    run = SyncRun(channel_id=channel.id, mode=mode); db.add(run); checkpoint(); db.commit()  # never hold the SQLite write lock across network calls
    found = queued = 0
    for metadata in YouTubeDataClient().list_uploads(YouTubeDataClient().resolve_channel(channel.url).uploads_playlist_id):
        if since and metadata["published_at"] and metadata["published_at"] < since: continue
        video = db.scalar(select(Video).where(Video.channel_id == channel.id, Video.youtube_video_id == metadata["youtube_video_id"]))
        if not video:
            video = Video(channel_id=channel.id, **metadata); db.add(video); db.flush(); found += 1
        else:
            for k, v in metadata.items(): setattr(video, k, v)
        eligible = mode == "all" or (mode == "retryable" and video.status in [VideoStatus.failed, VideoStatus.no_subtitle, VideoStatus.language_unavailable, VideoStatus.blocked]) or (mode in ["new", "since"] and video.status == VideoStatus.pending)
        if eligible and video.status != VideoStatus.completed:
            enqueue(db, "download", channel_id=channel.id, video_id=video.id, payload={"sync_run_id": run.id}); queued += 1
        checkpoint(); db.commit()
    channel.video_count = db.scalar(select(func.count(Video.id)).where(Video.channel_id == channel.id)) or 0
    channel.last_scanned_at = datetime.utcnow(); channel.next_sync_at = datetime.utcnow() + timedelta(hours=channel.settings.sync_interval_hours)
    run.new_videos, run.queued_videos, run.finished_at = found, queued, datetime.utcnow()
    return found, queued


def due_channel_syncs(db: Session):
    due = db.scalars(select(Channel).where(Channel.next_sync_at <= datetime.utcnow())).all()
    for channel in due: enqueue(db, "scan", channel_id=channel.id, payload={"mode": "new", "scheduled": True})
