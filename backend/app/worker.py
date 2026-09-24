"""Database-backed single-process worker. Run separately in production/Docker."""
import json, logging, time
from datetime import datetime, timedelta
from sqlalchemy import select
from .config import settings
from .database import SessionLocal
from .models import Channel, Job, JobStatus, Subtitle, SyncRun, Video, VideoStatus
from .services.jobs import due_channel_syncs, log, scan_channel
from .services.queue import Heartbeat, claim_next_job, new_worker_id, owns_job, recover_stale_jobs
from .services.storage import atomic_write_text, to_stored_path
from .services.subtitles import BlockedByYouTube, LanguageUnavailable, SubtitleUnavailable, fetch_selected, serialize, video_folder


def process_download(db, job: Job):
    video = db.get(Video, job.video_id); channel = db.get(Channel, job.channel_id)
    if not video or not channel: raise RuntimeError("Video hoặc channel không tồn tại")
    options = channel.settings
    video.status, video.last_processed_at = VideoStatus.processing, datetime.utcnow(); db.commit()  # release the write lock before network I/O
    transcript, translated, snippets = fetch_selected(video.youtube_video_id, options.preferred_languages, options.subtitle_preference, options.allow_translation)
    folder = video_folder(channel.title, video)
    paths = []
    for fmt in options.export_formats:
        path = folder / f"{transcript.language_code}.{fmt}"; atomic_write_text(path, serialize(snippets, fmt)); stored = to_stored_path(path)
        previous = db.scalar(select(Subtitle).where(Subtitle.video_id == video.id, Subtitle.language_code == transcript.language_code, Subtitle.format == fmt))
        if not previous:
            db.add(Subtitle(video_id=video.id, language=transcript.language, language_code=transcript.language_code, is_generated=transcript.is_generated, is_translatable=transcript.is_translatable, is_translated=translated, format=fmt, file_path=stored))
        else: previous.file_path = stored
        paths.append(stored)
    atomic_write_text(folder / "metadata.json", json.dumps({"video_id": video.youtube_video_id, "title": video.title, "url": video.url, "published_at": video.published_at.isoformat() if video.published_at else None, "subtitle": {"language": transcript.language, "language_code": transcript.language_code, "generated": transcript.is_generated, "translated": translated}, "files": paths}, ensure_ascii=False, indent=2))
    video.status, video.subtitle_path, video.last_error = VideoStatus.completed, paths[0] if paths else None, None
    if run_id := job.payload.get("sync_run_id"):
        run = db.get(SyncRun, run_id)
        if run: run.successful += 1


WORKER_ID = new_worker_id()
logger = logging.getLogger("app.worker")


def process_one() -> bool:
    """Claim and run one due job. Returns True if a job was picked up (so the caller should not sleep)."""
    db = SessionLocal()
    try:
        recover_stale_jobs(db)
        due_channel_syncs(db); db.commit()
        job = claim_next_job(db, WORKER_ID, settings.job_lease_seconds)
        if not job: return False
        with Heartbeat(SessionLocal, job.id, WORKER_ID, settings.job_lease_seconds):
            _run_job(db, job)
        return True
    finally: db.close()


def _run_job(db, job: Job):
    try:
        if job.kind == "scan": scan_channel(db, db.get(Channel, job.channel_id), job.payload.get("mode", "new"), job.payload.get("since"))
        elif job.kind == "download": process_download(db, job)
        else: raise RuntimeError(f"Loại job không hỗ trợ: {job.kind}")
        job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), None; log(db, job, "Hoàn tất")
    except SubtitleUnavailable as exc:
        db.rollback()
        v = db.get(Video, job.video_id); v.status, v.last_error, v.last_processed_at = VideoStatus.no_subtitle, str(exc), datetime.utcnow(); job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), str(exc); log(db, job, "Video không có subtitle")
        if run_id := job.payload.get("sync_run_id"):
            run = db.get(SyncRun, run_id)
            if run: run.no_subtitle += 1
    except LanguageUnavailable as exc:
        db.rollback()
        v = db.get(Video, job.video_id); v.status, v.last_error, v.last_processed_at = VideoStatus.language_unavailable, str(exc), datetime.utcnow(); job.status, job.finished_at, job.error = JobStatus.completed, datetime.utcnow(), str(exc); log(db, job, "Không có ngôn ngữ yêu cầu")
    except BlockedByYouTube as exc:
        db.rollback()
        v = db.get(Video, job.video_id); v.status, v.last_error = VideoStatus.blocked, str(exc); job.status, job.error, job.finished_at = JobStatus.failed, str(exc), datetime.utcnow(); log(db, job, "YouTube đã chặn request", "error")
    except Exception as exc:
        db.rollback()
        error = f"{type(exc).__name__}: {exc}"; v = db.get(Video, job.video_id) if job.video_id else None
        if v: v.retry_count, v.last_error, v.last_processed_at = v.retry_count + 1, error, datetime.utcnow()
        job.error = error
        if job.attempts < job.max_attempts:
            job.status, job.scheduled_at = JobStatus.queued, datetime.utcnow() + timedelta(seconds=min(300, 2 ** job.attempts * 5)); log(db, job, f"Lỗi tạm thời; thử lại: {error}", "warning")
            if v: v.status = VideoStatus.queued
        else:
            job.status, job.finished_at = JobStatus.failed, datetime.utcnow(); log(db, job, f"Thất bại: {error}", "error")
            if v: v.status = VideoStatus.failed
            if run_id := job.payload.get("sync_run_id"):
                run = db.get(SyncRun, run_id)
                if run: run.failed += 1
    if not owns_job(SessionLocal, job.id, WORKER_ID):
        db.rollback(); logger.warning("Job %s đã bị worker khác tiếp quản; bỏ kết quả", job.id); return
    job.worker_id, job.lease_expires_at = None, None
    db.commit()


def worker_loop(process=process_one, sleep=time.sleep, poll_seconds: float | None = None, should_stop=lambda: False):
    """Drain the queue back-to-back; sleep only when there is nothing to do (or after an error)."""
    poll = settings.worker_poll_seconds if poll_seconds is None else poll_seconds
    while not should_stop():
        try: worked = process()
        except Exception:
            logger.exception("Worker gặp lỗi không mong đợi"); worked = False
        if not worked: sleep(poll)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with SessionLocal() as startup_db: recover_stale_jobs(startup_db)
    worker_loop()
