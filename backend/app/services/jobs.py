import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Channel, Job, JobLog, JobStatus, SyncRun, Video, VideoStatus, uid
from .youtube import PlaylistNotFoundError, YouTubeDataClient

logger = logging.getLogger("app.sync")
ACTIVE_JOBS = (JobStatus.queued, JobStatus.processing, JobStatus.paused)
RETRYABLE_VIDEO_STATUSES = [VideoStatus.failed, VideoStatus.no_subtitle, VideoStatus.language_unavailable, VideoStatus.blocked]


def log(db: Session, job: Job | None, message: str, level: str = "info"):
    db.add(JobLog(job_id=job.id if job else None, message=message, level=level))


def _active_job(db: Session, kind: str, channel_id: str | None, video_id: str | None) -> Job | None:
    return db.scalar(select(Job).where(Job.kind == kind, Job.channel_id == channel_id, Job.video_id == video_id, Job.status.in_(ACTIVE_JOBS)).limit(1))


def enqueue(db: Session, kind: str, *, channel_id: str | None = None, video_id: str | None = None, payload: dict | None = None, sync_run_id: str | None = None) -> Job:
    """Create the job unless an active one exists for the same target. The partial unique indexes make
    that a DB guarantee: a concurrent insert loses via ON CONFLICT DO NOTHING instead of duplicating."""
    job = _active_job(db, kind, channel_id, video_id)
    if not job:
        now = datetime.utcnow(); job_id = uid()
        inserted = db.execute(sqlite_insert(Job).values(id=job_id, kind=kind, status=JobStatus.queued, channel_id=channel_id, video_id=video_id, payload=payload or {}, attempts=0,
                                                        max_attempts=settings.max_job_attempts, scheduled_at=now, created_at=now, sync_run_id=sync_run_id).on_conflict_do_nothing())
        job = db.get(Job, job_id) if inserted.rowcount == 1 else _active_job(db, kind, channel_id, video_id)
    if job and sync_run_id and job.sync_run_id is None:
        job.sync_run_id = sync_run_id  # adopt an unowned active job (e.g. a manual download) into this run
    if video_id:
        video = db.get(Video, video_id)
        if video and video.status != VideoStatus.completed: video.status = VideoStatus.queued
    return job


@dataclass
class ScanResult:
    first_video_id: str | None = None
    pages: int = 0
    inspected: int = 0
    new: int = 0
    queued: int = 0
    incremental: bool = False
    reached_end: bool = False
    advance_cursor: bool = False  # this scan verified enough to move the cursor
    history_verified: bool = False  # ... and to declare the whole history present in the DB
    reason: str = ""

    def summary(self, channel_title: str, mode: str) -> str:
        kind = "Incremental" if self.incremental else "Full"
        return f"{kind} scan {channel_title} (mode={mode}): {self.pages} pages, {self.inspected} inspected, {self.new} new, {self.queued} queued; {self.reason}"


def _resolve_playlist(db: Session, channel: Channel, client: YouTubeDataClient, checkpoint) -> str:
    """Network first, then persist: no SQLite write lock is held while talking to YouTube."""
    resolved = client.resolve_channel(f"https://www.youtube.com/channel/{channel.youtube_channel_id}")
    channel.uploads_playlist_id = resolved.uploads_playlist_id
    checkpoint(); db.commit()
    return resolved.uploads_playlist_id


def scan_channel(db: Session, channel: Channel, mode: str, since: datetime | None = None, checkpoint=None, run_id: str | None = None) -> ScanResult:
    """Scan the uploads playlist newest-first and store/queue what it finds.

    `checkpoint()` runs before every commit (the worker proves ownership there). Channel sync state
    (cursor) is NOT changed here; the caller applies it via apply_scan_result inside its guarded
    finalize transaction, so a failed or abandoned scan can never advance the cursor.
    """
    checkpoint = checkpoint or (lambda: None)
    if isinstance(since, str):
        from dateutil.parser import isoparse
        since = isoparse(since).replace(tzinfo=None)
    client = YouTubeDataClient()
    playlist_id = channel.uploads_playlist_id or _resolve_playlist(db, channel, client, checkpoint)
    try:
        return _scan_pages(db, channel, playlist_id, client, mode, since, checkpoint, run_id)
    except PlaylistNotFoundError:  # cached id went stale: re-resolve and retry exactly once
        logger.warning("Uploads playlist %s of %s no longer exists; re-resolving the channel", playlist_id, channel.title)
        playlist_id = _resolve_playlist(db, channel, client, checkpoint)
        return _scan_pages(db, channel, playlist_id, client, mode, since, checkpoint, run_id)


def _scan_pages(db: Session, channel: Channel, playlist_id: str, client, mode: str, since: datetime | None, checkpoint, run_id: str | None) -> ScanResult:
    cursor = channel.sync_cursor_video_id
    trusted = mode == "new" and since is None and bool(cursor and channel.history_complete_at)
    result = ScanResult(incremental=trusted)
    full_reason = ("no trusted cursor (first sync or legacy channel): scanning full history" if mode == "new" and since is None and not trusted else "")
    seen: set[str] = set(); cursor_found = False; known_run = 0; stopped = False

    for page in client.list_upload_pages(playlist_id):
        result.pages += 1
        entries = []
        for entry in page:
            if entry["youtube_video_id"] not in seen: seen.add(entry["youtube_video_id"]); entries.append(entry)
        if result.first_video_id is None and page: result.first_video_id = page[0]["youtube_video_id"]
        ids = [e["youtube_video_id"] for e in entries]
        known = set(db.scalars(select(Video.youtube_video_id).where(Video.channel_id == channel.id, Video.youtube_video_id.in_(ids)))) if ids else set()

        def wanted(entry):
            metadata = entry["metadata"]
            return metadata is not None and not (since and metadata["published_at"] and metadata["published_at"] < since)
        fresh = [{"id": uid(), "channel_id": channel.id, **e["metadata"]} for e in entries if wanted(e) and e["youtube_video_id"] not in known]
        if fresh: db.execute(sqlite_insert(Video).on_conflict_do_nothing(index_elements=["channel_id", "youtube_video_id"]), fresh)
        videos = {v.youtube_video_id: v for v in db.scalars(select(Video).where(Video.channel_id == channel.id, Video.youtube_video_id.in_(ids)))} if ids else {}

        page_new = 0
        for entry in entries:
            video_id = entry["youtube_video_id"]; result.inspected += 1
            if trusted:  # boundary bookkeeping runs on ids alone, so private/deleted entries still count
                if video_id == cursor: cursor_found, known_run = True, 0
                elif cursor_found and entry["metadata"] is not None: known_run = known_run + 1 if video_id in known else 0
            if not wanted(entry): continue
            video = videos.get(video_id)
            if video is None: continue
            if video_id in known:
                for key, value in entry["metadata"].items(): setattr(video, key, value)
            else: result.new += 1; page_new += 1
            eligible = mode == "all" or (mode == "retryable" and video.status in RETRYABLE_VIDEO_STATUSES) or (mode in ["new", "since"] and video.status == VideoStatus.pending)
            if eligible and video.status != VideoStatus.completed:
                enqueue(db, "download", channel_id=channel.id, video_id=video.id, sync_run_id=run_id); result.queued += 1
        if run_id and page_new:  # same transaction as the inserts above, so a retried scan can neither lose nor repeat this page's count
            db.execute(update(SyncRun).where(SyncRun.id == run_id).values(new_videos=SyncRun.new_videos + page_new))
        checkpoint(); db.commit()  # commit before the next page's network call

        if trusted and cursor_found and known_run >= settings.sync_safety_window:
            stopped = True; break

    result.reached_end = not stopped
    if stopped:
        result.advance_cursor = True  # history_verified stays as it was
        result.reason = f"stopped at known boundary (cursor + {known_run} known videos)"
    elif since is None and mode in ("new", "all", "retryable"):
        result.advance_cursor = result.history_verified = True
        result.reason = full_reason or ("cursor not found in playlist; scanned entire history" if trusted and not cursor_found else "scanned entire history")
    else:
        result.reason = "partial-history scan; sync state unchanged"
    logger.info(result.summary(channel.title, mode))
    return result


def apply_scan_result(db: Session, channel: Channel, result: ScanResult):
    """Must run inside the worker's guarded finalize transaction."""
    now = datetime.utcnow()
    if result.advance_cursor:
        channel.sync_cursor_video_id = result.first_video_id
        if result.history_verified: channel.history_complete_at = now
    channel.video_count = db.scalar(select(func.count(Video.id)).where(Video.channel_id == channel.id)) or 0
    channel.last_scanned_at, channel.next_sync_at = now, now + timedelta(hours=channel.settings.sync_interval_hours)


def due_channel_syncs(db: Session):
    due = db.scalars(select(Channel).where(Channel.next_sync_at <= datetime.utcnow())).all()
    for channel in due: enqueue(db, "scan", channel_id=channel.id, payload={"mode": "new", "scheduled": True})
