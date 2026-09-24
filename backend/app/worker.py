"""Database-backed single-process worker. Run separately in production/Docker.

Job lifecycle: claim (atomic CAS) -> network + file writes (no DB write lock held) ->
one guarded DB transaction that first proves ownership, then writes every result, then commits.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import Channel, Job, JobLog, JobStatus, Subtitle, SyncRun, SyncRunStatus, Video, VideoStatus
from .services.jobs import ScanResult, apply_scan_result, due_channel_syncs, scan_channel
from .services.maintenance import MaintenanceSchedule, run_retention
from .services.queue import Heartbeat, LostOwnership, claim_next_job, guard_job, new_worker_id, recover_stale_jobs
from .services.storage import atomic_write_text, to_stored_path
from .services.subtitles import BlockedByYouTube, LanguageUnavailable, SubtitleUnavailable, fetch_selected, serialize, video_folder
from .services.syncruns import refresh_sync_run_state
from .services.workers import STATE, HeartbeatThread

WORKER_ID = new_worker_id()
logger = logging.getLogger("app.worker")


@dataclass
class DownloadResult:
    """Everything the DB needs once the network fetch and file writes succeeded."""
    language: str; language_code: str; is_generated: bool; is_translatable: bool; translated: bool; stored_paths: dict[str, str]


def process_download(db, job: Job) -> DownloadResult:
    """Network fetch + atomic file writes. Result rows are written later by apply_download_result."""
    video = db.get(Video, job.video_id); channel = db.get(Channel, job.channel_id)
    if not video or not channel: raise RuntimeError("Video hoặc channel không tồn tại")
    options = channel.settings
    guard_job(db, job.id, WORKER_ID, job.attempts)
    video.status, video.last_processed_at = VideoStatus.processing, datetime.utcnow(); db.commit()  # release the write lock before network I/O
    transcript, translated, snippets = fetch_selected(video.youtube_video_id, options.preferred_languages, options.subtitle_preference, options.allow_translation)
    # Paths are deterministic per (video, language, format): a duplicate run rewrites identical files (idempotent).
    folder = video_folder(channel.title, video)
    stored = {}
    for fmt in options.export_formats:
        path = folder / f"{transcript.language_code}.{fmt}"; atomic_write_text(path, serialize(snippets, fmt)); stored[fmt] = to_stored_path(path)
    atomic_write_text(folder / "metadata.json", json.dumps({"video_id": video.youtube_video_id, "title": video.title, "url": video.url, "published_at": video.published_at.isoformat() if video.published_at else None, "subtitle": {"language": transcript.language, "language_code": transcript.language_code, "generated": transcript.is_generated, "translated": translated}, "files": list(stored.values())}, ensure_ascii=False, indent=2))
    return DownloadResult(transcript.language, transcript.language_code, transcript.is_generated, transcript.is_translatable, translated, stored)


def apply_download_result(db, job_video_id: str, result: DownloadResult):
    video = db.get(Video, job_video_id)
    for fmt, stored in result.stored_paths.items():
        previous = db.scalar(select(Subtitle).where(Subtitle.video_id == video.id, Subtitle.language_code == result.language_code, Subtitle.format == fmt))
        if not previous:
            db.add(Subtitle(video_id=video.id, language=result.language, language_code=result.language_code, is_generated=result.is_generated, is_translatable=result.is_translatable, is_translated=result.translated, format=fmt, file_path=stored))
        else: previous.file_path = stored
    first = next(iter(result.stored_paths.values()), None)
    video.status, video.subtitle_path, video.last_error, video.last_processed_at = VideoStatus.completed, first, None, datetime.utcnow()


def process_one() -> bool:
    """Claim and run one due job. Returns True if a job was picked up (so the caller should not sleep)."""
    db = SessionLocal()
    try:
        recover_stale_jobs(db)
        due_channel_syncs(db); db.commit()
        job = claim_next_job(db, WORKER_ID, settings.job_lease_seconds)
        if not job: return False
        STATE.busy(job.id)
        with Heartbeat(SessionLocal, job.id, WORKER_ID, settings.job_lease_seconds, settings.job_max_runtime_seconds):
            try: _run_job(db, job)
            except LostOwnership:
                db.rollback(); logger.warning("Job %s đã bị worker khác tiếp quản; bỏ toàn bộ kết quả", job.id)
        return True
    finally:
        STATE.idle(); db.close()


def _run_job(db, job: Job):
    """Raises LostOwnership when this claim is no longer the owner at any guarded write."""
    job_id, attempts, max_attempts, video_id, channel_id = job.id, job.attempts, job.max_attempts, job.video_id, job.channel_id  # snapshot: rollbacks expire ORM state
    run_id = job.sync_run_id
    now = datetime.utcnow
    def guard(**values): guard_job(db, job_id, WORKER_ID, attempts, **values)
    def log_job(message: str, level: str = "info"): db.add(JobLog(job_id=job_id, message=message, level=level))
    def video_row(): return db.get(Video, video_id) if video_id else None
    def finalize(apply=None, **values):
        """One transaction: ownership CAS first, then every result write, then the SyncRun refresh, then commit."""
        guard(worker_id=None, lease_expires_at=None, **values)
        if apply: apply()
        db.flush()
        refresh_sync_run_state(db, run_id)
        db.commit()

    try:
        result = None
        if job.kind == "scan":
            if not run_id:  # first attempt (or a job queued before SyncRun linkage existed); a retry reuses the same run
                run = SyncRun(channel_id=job.channel_id, mode=job.payload.get("mode", "new"), status=SyncRunStatus.scanning.value); db.add(run); db.flush()  # row must exist before the job FK points at it
                guard(sync_run_id=run.id); db.commit(); run_id = run.id
            channel = db.get(Channel, job.channel_id)
            result = scan_channel(db, channel, job.payload.get("mode", "new"), job.payload.get("since"), checkpoint=guard, run_id=run_id)
        elif job.kind == "download": result = process_download(db, job)
        else: raise RuntimeError(f"Loại job không hỗ trợ: {job.kind}")
        def done():
            if isinstance(result, DownloadResult): apply_download_result(db, video_id, result)
            if isinstance(result, ScanResult):
                apply_scan_result(db, channel, result); log_job(result.summary(channel.title, job.payload.get("mode", "new")))
            log_job("Hoàn tất")
        finalize(done, status=JobStatus.completed, finished_at=now(), error=None, outcome="success" if job.kind == "download" else None)
    except LostOwnership: raise
    except SubtitleUnavailable as exc:
        db.rollback()
        reason = str(exc)
        def apply():
            v = video_row(); v.status, v.last_error, v.last_processed_at = VideoStatus.no_subtitle, reason, now(); log_job("Video không có subtitle")
        finalize(apply, status=JobStatus.completed, finished_at=now(), error=reason, outcome="no_subtitle")
    except LanguageUnavailable as exc:
        db.rollback()
        reason = str(exc)
        def apply():
            v = video_row(); v.status, v.last_error, v.last_processed_at = VideoStatus.language_unavailable, reason, now(); log_job("Không có ngôn ngữ yêu cầu")
        finalize(apply, status=JobStatus.completed, finished_at=now(), error=reason, outcome="language_unavailable")
    except BlockedByYouTube as exc:
        db.rollback()
        reason = str(exc)
        def apply():
            v = video_row(); v.status, v.last_error = VideoStatus.blocked, reason; log_job("YouTube đã chặn request", "error")
        finalize(apply, status=JobStatus.failed, finished_at=now(), error=reason, outcome="blocked")
    except Exception as exc:
        db.rollback()
        error = f"{type(exc).__name__}: {exc}"
        if attempts < max_attempts:
            def apply():
                v = video_row()
                if v: v.retry_count, v.last_error, v.last_processed_at, v.status = v.retry_count + 1, error, now(), VideoStatus.queued
                log_job(f"Lỗi tạm thời; thử lại: {error}", "warning")
            finalize(apply, status=JobStatus.queued, error=error, outcome=None, scheduled_at=now() + timedelta(seconds=min(300, 2 ** attempts * 5)))
        else:
            def apply():
                v = video_row()
                if v: v.retry_count, v.last_error, v.last_processed_at, v.status = v.retry_count + 1, error, now(), VideoStatus.failed
                log_job(f"Thất bại: {error}", "error")
                if job.kind == "scan":  # a scheduled channel must not be re-enqueued immediately after a permanent scan failure
                    channel = db.get(Channel, channel_id)
                    if channel and channel.next_sync_at is not None:
                        channel.next_sync_at = max(channel.next_sync_at, now() + timedelta(minutes=settings.scan_failure_retry_minutes))
            finalize(apply, status=JobStatus.failed, finished_at=now(), error=error, outcome="failed" if job.kind == "download" else None)


def run_maintenance():
    with SessionLocal() as db: return run_retention(db)


def worker_loop(process=process_one, sleep=time.sleep, poll_seconds: float | None = None, should_stop=lambda: False, maintenance=None, schedule=None):
    """Drain the queue back-to-back; sleep only when there is nothing to do (or after an error).
    `maintenance` (retention) runs at startup and then every MAINTENANCE_INTERVAL_HOURS, never per poll."""
    poll = settings.worker_poll_seconds if poll_seconds is None else poll_seconds
    schedule = schedule or MaintenanceSchedule()
    while not should_stop():
        STATE.tick()
        if maintenance and schedule.due():
            STATE.busy("maintenance")
            try: maintenance()
            except Exception: logger.exception("Maintenance lỗi")
            finally: schedule.done(); STATE.idle()
        try: worked = process()
        except Exception:
            logger.exception("Worker gặp lỗi không mong đợi"); worked = False
        if not worked: sleep(poll)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Worker %s khởi động", WORKER_ID)
    heartbeat = HeartbeatThread(SessionLocal, WORKER_ID).start()
    try:
        with SessionLocal() as startup_db: recover_stale_jobs(startup_db)
        worker_loop(maintenance=run_maintenance)
    finally:
        heartbeat.stop()
